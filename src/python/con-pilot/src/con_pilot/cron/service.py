"""Scheduling, cron-job CRUD and pending-log dispatch.

Pure module functions. ``ConPilot`` builds a :class:`CronContext` that
exposes the exact subset of pilot state these functions need (config,
paths, scheduler slot accessors, dispatcher peek). The functions never
reach into pilot internals via ``self``.
"""

from __future__ import annotations

import json
import os
import threading
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from con_pilot.agents.service import expand_name
from con_pilot.logger import app_logger

log = app_logger.bind(module=__name__)

# Guard against AttributeError when _eventloop is cleared to None by shutdown()
# while a job-completion wakeup callback is still queued in the event loop.
# Patching at the class level means every instance is protected, including
# callbacks already registered via call_later before shutdown completes.
#
# Two failure modes:
#  1. wakeup() fires from a thread *after* _eventloop=None: the run_in_event_loop
#     decorator crashes on call_soon_threadsafe. Guard by wrapping wakeup.
#  2. wakeup() body runs in the loop *after* _eventloop=None: _start_timer crashes.
#     Guard by wrapping _start_timer.
_orig_start_timer = AsyncIOScheduler._start_timer
_orig_wakeup = AsyncIOScheduler.wakeup


def _safe_start_timer(self: AsyncIOScheduler, wait_seconds: float | None) -> None:
    if getattr(self, "_eventloop", None) is None:
        return
    _orig_start_timer(self, wait_seconds)


def _safe_wakeup(self: AsyncIOScheduler) -> None:
    if getattr(self, "_eventloop", None) is None:
        return
    _orig_wakeup(self)


AsyncIOScheduler._start_timer = _safe_start_timer  # type: ignore[method-assign]
AsyncIOScheduler.wakeup = _safe_wakeup  # type: ignore[method-assign]


_CRON_PLACEHOLDER = """\
# Cron jobs for the {role} agent.
# Format: TOML — add [[job]] blocks with name, schedule (cron expression), and task.
#
# Example:
# [[job]]
# name = "daily-check"
# schedule = "0 9 * * *"
# task = "Describe what the {role} agent should do at this time."
"""


_CRON_TASK_MUTABLE_FIELDS: tuple[str, ...] = (
    "agent",
    "description",
    "instructions",
    "cron",
    "create_on_ping",
    "permissions",
)


def _run_persisted_task_job(conductor_home: str, task_name: str) -> None:
    """APScheduler job entrypoint that can be serialized in SQLAlchemyJobStore."""
    # Lazy import avoids a circular import (conductor imports this module).
    from con_pilot.conductor import ConPilot

    pilot = ConPilot(conductor_home=conductor_home)
    pilot._queue_task_from_scheduler(task_name)


# ── Context ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CronContext:
    """Explicit dependency surface for the cron service functions.

    Built by :class:`con_pilot.conductor.ConPilot` and passed as the first
    argument to every public function in this module. Keeping it frozen
    and explicit prevents the cron logic from reaching into pilot
    internals via ``self``.
    """

    home: str
    config_path: str
    cron_dir: str
    cron_state_dir: str
    pending_log_path: str
    scheduler_db_path: str
    scheduler_lock: threading.RLock
    get_config: Callable[[], Any]
    reload_config: Callable[[], Any]
    get_scheduler: Callable[[], AsyncIOScheduler]
    peek_scheduler: Callable[[], AsyncIOScheduler | None]
    clear_scheduler: Callable[[], None]
    role_cron_root: Callable[[str, str | None], str]
    project_cron_dir: Callable[[str], str]
    peek_dispatcher: Callable[[], Any]


# ── Internal helpers ──────────────────────────────────────────────────────
def _scheduler_job_id(task_name: str) -> str:
    return f"task::{task_name}"


def _cron_trigger(expression: str) -> CronTrigger:
    parts = expression.split()
    if len(parts) == 5:
        minute, hour, day, month, day_of_week = parts
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=UTC,
        )
    if len(parts) == 6:
        second, minute, hour, day, month, day_of_week = parts
        return CronTrigger(
            second=second,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=UTC,
        )
    raise ValueError(
        f"Cron expression must have 5 or 6 fields, got {len(parts)}: {expression!r}"
    )


def _persist_config(ctx: CronContext) -> None:
    """Write the current in-memory config back to ``ctx.config_path``."""
    cfg = ctx.get_config()
    payload = cfg.model_dump(mode="json", by_alias=True, exclude_none=True)
    with open(ctx.config_path, "w") as f:
        if ctx.config_path.endswith((".yaml", ".yml")):
            yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)
        else:
            json.dump(payload, f, indent=2)


def _refresh_scheduled_task_jobs(ctx: CronContext) -> None:
    """Mirror all config.tasks with cron expressions into APScheduler jobs."""
    cfg = ctx.reload_config()
    desired_job_ids: set[str] = set()
    scheduler = ctx.get_scheduler()

    for task in cfg.scheduled_tasks:
        job_id = _scheduler_job_id(task.name)
        desired_job_ids.add(job_id)

        try:
            trigger = _cron_trigger(task.cron or "")
        except ValueError as exc:
            log.warning(
                "Skipping task %s due to invalid cron expression %r: %s",
                task.name,
                task.cron,
                exc,
            )
            continue

        scheduler.add_job(
            _run_persisted_task_job,
            trigger=trigger,
            id=job_id,
            replace_existing=True,
            kwargs={"conductor_home": ctx.home, "task_name": task.name},
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )

    for job in scheduler.get_jobs():
        if job.id.startswith("task::") and job.id not in desired_job_ids:
            scheduler.remove_job(job.id)


def _reschedule_after_config_change(ctx: CronContext) -> None:
    """Mirror the refreshed config into APScheduler if it is running."""
    with ctx.scheduler_lock:
        scheduler = ctx.peek_scheduler()
        if scheduler is None or not scheduler.running:
            return
    _refresh_scheduled_task_jobs(ctx)


def _describe_cron_job(ctx: CronContext, task_name: str) -> dict[str, Any] | None:
    """Return a dict describing a configured cron task and its scheduler state."""
    from con_pilot.conductor.models import TaskConfig

    cfg = ctx.get_config()
    task: TaskConfig | None = next((t for t in cfg.tasks if t.name == task_name), None)
    if task is None:
        return None

    job_id = _scheduler_job_id(task.name)
    next_run: str | None = None
    registered = False
    with ctx.scheduler_lock:
        scheduler = ctx.peek_scheduler()
        if scheduler is not None:
            job = scheduler.get_job(job_id)
            if job is not None:
                registered = True
                if job.next_run_time is not None:
                    next_run = job.next_run_time.isoformat()

    return {
        "name": task.name,
        "agent": task.agent,
        "description": task.description,
        "instructions": task.instructions,
        "cron": task.cron,
        "create_on_ping": task.create_on_ping,
        "permissions": list(task.permissions) if task.permissions else None,
        "scheduled": task.cron is not None,
        "registered": registered,
        "next_run_time": next_run,
        "job_id": job_id if registered else None,
    }


def _cron_state_path(
    ctx: CronContext, role: str, job_name: str, project: str | None = None
) -> str:
    root = ctx.role_cron_root(role, project)
    return os.path.join(root, ".state", f"{role}__{job_name}.last_run")


def _last_run(
    ctx: CronContext, role: str, job_name: str, project: str | None = None
) -> datetime:
    path = _cron_state_path(ctx, role, job_name, project)
    if not os.path.exists(path):
        return datetime.fromtimestamp(0, tz=UTC)
    with open(path) as f:
        return datetime.fromisoformat(f.read().strip())


def _save_last_run(
    ctx: CronContext,
    role: str,
    job_name: str,
    dt: datetime,
    project: str | None = None,
) -> None:
    state_dir = os.path.join(ctx.role_cron_root(role, project), ".state")
    os.makedirs(state_dir, exist_ok=True)
    with open(_cron_state_path(ctx, role, job_name, project), "w") as f:
        f.write(dt.isoformat())


def _cron_is_due(schedule: str, last_run: datetime) -> bool:
    now = datetime.now(tz=UTC)
    cron = croniter(schedule, last_run.replace(tzinfo=None))
    next_run = cron.get_next(datetime).replace(tzinfo=UTC)
    return now >= next_run


def _append_pending(
    ctx: CronContext,
    role: str,
    agent_name: str,
    job_name: str,
    task: str,
    schedule: str,
    *,
    project: str | None = None,
) -> None:
    cron_dir = ctx.role_cron_root(role, project)
    os.makedirs(cron_dir, exist_ok=True)
    pending_log = os.path.join(cron_dir, "pending.log")
    now = datetime.now(tz=UTC).isoformat()
    with open(pending_log, "a") as f:
        f.write(
            f"[{now}] role={role} agent={agent_name} job={job_name} "
            f"schedule={schedule!r}\n  task: {task}\n\n"
        )
    # Wake the dispatcher (no-op if not running).
    dispatcher = ctx.peek_dispatcher()
    if dispatcher is not None:
        dispatcher.notify()


def _queue_task(ctx: CronContext, task_name: str, *, source: str = "manual") -> bool:
    """Append a task into the appropriate pending.log. Returns True if queued."""
    cfg = ctx.reload_config()
    task = next((t for t in cfg.tasks if t.name == task_name), None)
    if task is None:
        log.warning("Task not found: %s", task_name)
        return False

    role = task.agent
    role_cfg = cfg.get_agent_dict(role)
    if not role_cfg:
        log.warning("Skipping task %s: unknown agent role %s", task.name, role)
        return False

    if not role_cfg.get("active", role == "conductor"):
        log.info("Skipping task %s: agent role %s is inactive", task.name, role)
        return False

    role_project: str | None = None
    if role_cfg.get("scope", "system") == "project":
        role_project = os.environ.get("PROJECT_NAME") or None
        if not role_project:
            log.warning(
                "Skipping task %s: PROJECT_NAME is not set for project-scoped role %s",
                task.name,
                role,
            )
            return False

    agent_name = role_cfg.get("name", role)
    if role_project:
        agent_name = expand_name(agent_name, project=role_project)

    _append_pending(
        ctx,
        role,
        agent_name,
        task.name,
        task.instructions,
        task.cron or "manual",
        project=role_project,
    )
    log.info(
        "Queued task (%s): [%s] %s — %s...",
        source,
        role,
        task.name,
        task.instructions[:60],
    )
    return True


# ── Public API ────────────────────────────────────────────────────────────
def queue_task_from_scheduler(ctx: CronContext, task_name: str) -> None:
    """Queue a scheduled top-level task into pending.log for the target agent."""
    cfg = ctx.reload_config()
    task = next((t for t in cfg.tasks if t.name == task_name), None)
    if task is None or task.cron is None:
        log.warning("Scheduled task not found or not cron-enabled: %s", task_name)
        return
    _queue_task(ctx, task_name, source="scheduler")


def run_task(ctx: CronContext, task_name: str) -> bool:
    """Queue a configured task for execution by appending it to ``pending.log``."""
    return _queue_task(ctx, task_name, source="manual")


async def start_scheduler(ctx: CronContext) -> None:
    """Start APScheduler on the running event loop and load configured tasks."""
    with ctx.scheduler_lock:
        scheduler = ctx.get_scheduler()
        if scheduler.running:
            return
        _refresh_scheduled_task_jobs(ctx)
        scheduler.start()
        log.info(
            "APScheduler started (db=%s, jobs=%d)",
            ctx.scheduler_db_path,
            len(scheduler.get_jobs()),
        )


async def stop_scheduler(ctx: CronContext) -> None:
    """Stop APScheduler without waiting for in-flight jobs."""
    with ctx.scheduler_lock:
        scheduler = ctx.peek_scheduler()
        if scheduler is None or not scheduler.running:
            return
        scheduler.shutdown(wait=False)
        ctx.clear_scheduler()
        log.info("APScheduler stopped")


def list_cron_jobs(ctx: CronContext) -> list[dict[str, Any]]:
    """List every configured task with its scheduler registration state."""
    return [_describe_cron_job(ctx, task.name) or {} for task in ctx.get_config().tasks]


def get_cron_job(ctx: CronContext, name: str) -> dict[str, Any] | None:
    """Return the description of a single configured task."""
    return _describe_cron_job(ctx, name)


def read_cron_logs(
    ctx: CronContext, *, lines: int | None = None, project: str | None = None
) -> dict[str, Any]:
    """Return tail lines from a cron ``pending.log`` file."""
    if project:
        log_path = os.path.join(ctx.project_cron_dir(project), "pending.log")
    else:
        log_path = ctx.pending_log_path

    if not os.path.exists(log_path):
        return {"path": log_path, "lines": [], "total": 0}

    with open(log_path) as f:
        all_lines = f.readlines()

    total = len(all_lines)
    selected = all_lines if lines is None else all_lines[-lines:]
    return {
        "path": log_path,
        "total": total,
        "lines": [line.rstrip("\n") for line in selected],
    }


def add_cron_job(ctx: CronContext, task_data: dict[str, Any]) -> dict[str, Any]:
    """Append a new task to ``conductor.yaml`` and refresh the scheduler."""
    from con_pilot.conductor.models import TaskConfig

    required = ("name", "agent", "description", "instructions")
    missing = [f for f in required if not task_data.get(f)]
    if missing:
        raise ValueError(f"Missing required field(s): {', '.join(missing)}")

    try:
        new_task = TaskConfig(**task_data)
    except Exception as exc:
        raise ValueError(f"Invalid task definition: {exc}") from exc

    cfg = ctx.get_config()
    if any(t.name == new_task.name for t in cfg.tasks):
        raise ValueError(f"Task '{new_task.name}' already exists")
    if new_task.agent not in cfg.agents:
        raise ValueError(f"Unknown agent role: {new_task.agent}")

    cfg.tasks.append(new_task)
    _persist_config(ctx)
    ctx.reload_config()
    _reschedule_after_config_change(ctx)

    result = _describe_cron_job(ctx, new_task.name)
    assert result is not None
    return result


def update_cron_job(
    ctx: CronContext, name: str, changes: dict[str, Any]
) -> dict[str, Any] | None:
    """Update mutable fields of an existing task and refresh the scheduler."""
    from con_pilot.conductor.models import TaskConfig

    unknown = [k for k in changes if k not in _CRON_TASK_MUTABLE_FIELDS]
    if unknown:
        raise ValueError(f"Unsupported field(s): {', '.join(sorted(unknown))}")
    if not changes:
        raise ValueError("No changes provided")

    cfg = ctx.get_config()
    idx = next((i for i, t in enumerate(cfg.tasks) if t.name == name), None)
    if idx is None:
        return None

    existing = cfg.tasks[idx]
    merged = existing.model_dump()
    merged.update(changes)
    try:
        updated = TaskConfig(**merged)
    except Exception as exc:
        raise ValueError(f"Invalid task definition: {exc}") from exc

    if updated.agent not in cfg.agents:
        raise ValueError(f"Unknown agent role: {updated.agent}")

    cfg.tasks[idx] = updated
    _persist_config(ctx)
    ctx.reload_config()
    _reschedule_after_config_change(ctx)
    return _describe_cron_job(ctx, name)


def remove_cron_job(ctx: CronContext, name: str) -> bool:
    """Remove a task from configuration and unregister its scheduler job."""
    cfg = ctx.get_config()
    before = len(cfg.tasks)
    cfg.tasks = [t for t in cfg.tasks if t.name != name]
    if len(cfg.tasks) == before:
        return False

    _persist_config(ctx)
    ctx.reload_config()

    with ctx.scheduler_lock:
        scheduler = ctx.peek_scheduler()
        if scheduler is not None and scheduler.running:
            try:
                scheduler.remove_job(_scheduler_job_id(name))
            except Exception as exc:  # noqa: BLE001
                log.debug("remove_job(%s) failed: %s", name, exc)
    _reschedule_after_config_change(ctx)
    return True


def cron_sweep(ctx: CronContext, project: str | None = None) -> None:
    """Walk every active role and queue any cron jobs that are due."""
    # Ensure system cron dirs exist
    os.makedirs(ctx.cron_dir, exist_ok=True)
    os.makedirs(ctx.cron_state_dir, exist_ok=True)

    for role, role_cfg in ctx.get_config().agent_dicts.items():
        cron_cfg_raw = role_cfg.get("cron")
        if not cron_cfg_raw:
            continue
        if not role_cfg.get("active", role == "conductor"):
            continue

        scope = role_cfg.get("scope", "system")
        role_project = project if scope == "project" else None
        cron_dir = ctx.role_cron_root(role, role_project)
        os.makedirs(cron_dir, exist_ok=True)
        os.makedirs(os.path.join(cron_dir, ".state"), exist_ok=True)

        cron_file = os.path.join(cron_dir, f"{role}.cron")
        if not os.path.exists(cron_file):
            with open(cron_file, "w") as f:
                f.write(_CRON_PLACEHOLDER.format(role=role))
            log.info("Created placeholder cron file: %s.cron", role)
            continue

        try:
            with open(cron_file, "rb") as f:
                cron_cfg = tomllib.load(f)
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to parse %s.cron: %s", role, exc)
            continue

        agent_name = role_cfg.get("name", role)
        for job in cron_cfg.get("job", []):
            job_name = job.get("name", "unnamed")
            schedule = job.get("schedule", "")
            task = job.get("task", "")
            if not schedule or not task:
                continue
            last = _last_run(ctx, role, job_name, role_project)
            if _cron_is_due(schedule, last):
                now = datetime.now(tz=UTC)
                _append_pending(
                    ctx,
                    role,
                    agent_name,
                    job_name,
                    task,
                    schedule,
                    project=role_project,
                )
                _save_last_run(ctx, role, job_name, now, role_project)
                log.info("Queued: [%s] %s — %s...", role, job_name, task[:60])


def refresh_scheduled_task_jobs(ctx: CronContext) -> None:
    """Public wrapper for sync()'s post-write reschedule path."""
    _refresh_scheduled_task_jobs(ctx)
