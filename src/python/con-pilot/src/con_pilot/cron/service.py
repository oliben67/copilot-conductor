"""Scheduling, cron-job CRUD and pending-log dispatch.

Mixed into :class:`con_pilot.conductor.ConPilot`. Relies on attributes
defined on ``ConPilot``: ``self.scheduler`` /
``self.scheduler_db_path`` / ``self._scheduler_lock`` (APScheduler
state), ``self.config`` / ``self.reload_config`` / ``self._cfg``
(configuration), ``self.cron_dir`` / ``self.cron_state_dir`` /
``self.pending_log`` (paths), ``self._role_cron_root`` and
``self._dispatcher`` (helpers).
"""

from __future__ import annotations

import json
import os
import tomllib
from datetime import UTC, datetime
from typing import Any

import yaml
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from con_pilot.logger import app_logger

log = app_logger.bind(module=__name__)


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


def _run_persisted_task_job(conductor_home: str, task_name: str) -> None:
    """APScheduler job entrypoint that can be serialized in SQLAlchemyJobStore."""
    # Lazy import avoids a circular import (conductor imports this module).
    from con_pilot.conductor import ConPilot

    pilot = ConPilot(conductor_home=conductor_home, require_token=False)
    pilot._queue_task_from_scheduler(task_name)


class CronFacet:
    """Mixin grouping APScheduler control and cron-job management."""

    # ── Cron ───────────────────────────────────────────────────────────────────

    def _scheduler_job_id(self, task_name: str) -> str:
        return f"task::{task_name}"

    def _cron_trigger(self, expression: str) -> CronTrigger:
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

    def _queue_task_from_scheduler(self, task_name: str) -> None:
        """Queue a scheduled top-level task into pending.log for the target agent."""
        cfg = self.reload_config()
        task = next((t for t in cfg.tasks if t.name == task_name), None)
        if task is None or task.cron is None:
            log.warning("Scheduled task not found or not cron-enabled: %s", task_name)
            return
        self._queue_task(task_name, source="scheduler")

    def _queue_task(self, task_name: str, *, source: str = "manual") -> bool:
        """Append a task into the appropriate pending.log. Returns True if queued."""
        cfg = self.reload_config()
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
            agent_name = self._expand_name(agent_name, project=role_project)

        self._append_pending(
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

    def run_task(self, task_name: str) -> bool:
        """
        Queue a configured task for execution by appending it to ``pending.log``.

        Example:
            queued = pilot.run_task("agent-health-check")

        Note:
            Works for both scheduled (cron) and manual tasks. The pending
            dispatcher (when running) will pick the entry up on its next tick;
            project-scoped tasks require ``PROJECT_NAME`` to be set.

        :param task_name: name of a task declared in ``conductor.yaml``.
        :type task_name: `str`
        :return: ``True`` when the task was queued, ``False`` when it does
            not exist, is inactive, or is missing required project context.
        :rtype: `bool`
        """
        return self._queue_task(task_name, source="manual")

    def _refresh_scheduled_task_jobs(self) -> None:
        """Mirror all config.tasks with cron expressions into APScheduler jobs."""
        cfg = self.reload_config()
        desired_job_ids: set[str] = set()

        for task in cfg.scheduled_tasks:
            job_id = self._scheduler_job_id(task.name)
            desired_job_ids.add(job_id)

            try:
                trigger = self._cron_trigger(task.cron or "")
            except ValueError as exc:
                log.warning(
                    "Skipping task %s due to invalid cron expression %r: %s",
                    task.name,
                    task.cron,
                    exc,
                )
                continue

            self.scheduler.add_job(
                _run_persisted_task_job,
                trigger=trigger,
                id=job_id,
                replace_existing=True,
                kwargs={"conductor_home": self.home, "task_name": task.name},
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )

        for job in self.scheduler.get_jobs():
            if job.id.startswith("task::") and job.id not in desired_job_ids:
                self.scheduler.remove_job(job.id)

    async def start_scheduler(self) -> None:
        """
        Start APScheduler on the running event loop and load configured tasks.

        Example:
            await pilot.start_scheduler()

        Note:
            Idempotent: returns early when the scheduler is already running.
            All tasks declared in ``conductor.yaml`` with a ``cron`` expression
            are mirrored into APScheduler jobs prefixed with ``task::``.

        :return: None
        :rtype: `None`
        """
        with self._scheduler_lock:
            if self.scheduler.running:
                return
            self._refresh_scheduled_task_jobs()
            self.scheduler.start()
            log.info(
                "APScheduler started (db=%s, jobs=%d)",
                self.scheduler_db_path,
                len(self.scheduler.get_jobs()),
            )

    async def stop_scheduler(self) -> None:
        """
        Stop APScheduler without waiting for in-flight jobs.

        Example:
            await pilot.stop_scheduler()

        Note:
            Idempotent: returns early when no scheduler is running.

        :return: None
        :rtype: `None`
        """
        with self._scheduler_lock:
            if self._scheduler is None or not self._scheduler.running:
                return
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            log.info("APScheduler stopped")

    # ── Cron job management (API surface) ──────────────────────────────────────

    _CRON_TASK_MUTABLE_FIELDS: tuple[str, ...] = (
        "agent",
        "description",
        "instructions",
        "cron",
        "create_on_ping",
        "permissions",
    )

    def _describe_cron_job(self, task_name: str) -> dict[str, Any] | None:
        """Return a dict describing a configured cron task and its scheduler state."""
        from con_pilot.conductor.models import TaskConfig

        cfg = self.config
        task: TaskConfig | None = next(
            (t for t in cfg.tasks if t.name == task_name), None
        )
        if task is None:
            return None

        job_id = self._scheduler_job_id(task.name)
        next_run: str | None = None
        registered = False
        with self._scheduler_lock:
            scheduler = self._scheduler
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

    def list_cron_jobs(self) -> list[dict[str, Any]]:
        """
        List every configured task with its scheduler registration state.

        Example:
            for job in pilot.list_cron_jobs():
                print(job["name"], job["scheduled"], job["registered"])

        :return: a list of dicts with keys ``name``, ``agent``, ``description``,
            ``instructions``, ``cron``, ``create_on_ping``, ``permissions``,
            ``scheduled``, ``registered``, ``next_run_time`` and ``job_id``.
        :rtype: `list[dict[str, Any]]`
        """
        return [self._describe_cron_job(task.name) or {} for task in self.config.tasks]

    def get_cron_job(self, name: str) -> dict[str, Any] | None:
        """
        Return the description of a single configured task.

        :param name: task name as declared in ``conductor.yaml``.
        :type name: `str`
        :return: the same shape as one entry of :meth:`list_cron_jobs`, or
            ``None`` when no task with that name exists.
        :rtype: `dict[str, Any] | None`
        """
        return self._describe_cron_job(name)

    def read_cron_logs(
        self, *, lines: int | None = None, project: str | None = None
    ) -> dict[str, Any]:
        """
        Return tail lines from a cron ``pending.log`` file.

        Example:
            tail = pilot.read_cron_logs(lines=100)

        Note:
            When ``project`` is provided the project-scoped log under
            ``.github/projects/{project}/cron/pending.log`` is used; otherwise
            the system-level ``pending.log`` is read.

        :param lines: maximum number of trailing lines to return; ``None``
            returns the full file.
        :type lines: `int | None`
        :param project: optional project name selecting a project-scoped log.
        :type project: `str | None`
        :return: a mapping with keys ``path``, ``total`` (line count) and
            ``lines`` (selected lines without trailing newlines).
        :rtype: `dict[str, Any]`
        """
        if project:
            log_path = os.path.join(self.project_cron_dir(project), "pending.log")
        else:
            log_path = self.pending_log

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

    def _persist_config(self) -> None:
        """Write the current in-memory config back to ``self.config_path``."""
        payload = self.config.model_dump(mode="json", by_alias=True, exclude_none=True)
        with open(self.config_path, "w") as f:
            if self.config_path.endswith((".yaml", ".yml")):
                yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)
            else:
                json.dump(payload, f, indent=2)

    def _reschedule_after_config_change(self) -> None:
        """Mirror the refreshed config into APScheduler if it is running."""
        with self._scheduler_lock:
            scheduler = self._scheduler
            if scheduler is None or not scheduler.running:
                return
        self._refresh_scheduled_task_jobs()

    def add_cron_job(self, task_data: dict[str, Any]) -> dict[str, Any]:
        """
        Append a new task to ``conductor.yaml`` and refresh the scheduler.

        Example:
            pilot.add_cron_job({
                "name": "nightly", "agent": "developer",
                "description": "...", "instructions": "...",
                "cron": "0 2 * * *",
            })

        :param task_data: dict matching the :class:`TaskConfig` schema; the
            keys ``name``, ``agent``, ``description`` and ``instructions``
            are required.
        :type task_data: `dict[str, Any]`
        :return: the description of the newly-added job (same shape as
            :meth:`get_cron_job`).
        :rtype: `dict[str, Any]`
        :raises ValueError: when required fields are missing, the payload
            fails validation, the task name already exists, or ``agent`` is
            not a configured role.
        """
        from con_pilot.conductor.models import TaskConfig

        required = ("name", "agent", "description", "instructions")
        missing = [f for f in required if not task_data.get(f)]
        if missing:
            raise ValueError(f"Missing required field(s): {', '.join(missing)}")

        try:
            new_task = TaskConfig(**task_data)
        except Exception as exc:
            raise ValueError(f"Invalid task definition: {exc}") from exc

        cfg = self.config
        if any(t.name == new_task.name for t in cfg.tasks):
            raise ValueError(f"Task '{new_task.name}' already exists")
        if new_task.agent not in cfg.agents:
            raise ValueError(f"Unknown agent role: {new_task.agent}")

        cfg.tasks.append(new_task)
        self._persist_config()
        self.reload_config()
        self._reschedule_after_config_change()

        result = self._describe_cron_job(new_task.name)
        assert result is not None
        return result

    def update_cron_job(
        self, name: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Update mutable fields of an existing task and refresh the scheduler.

        Example:
            pilot.update_cron_job("nightly", {"cron": "0 3 * * *"})

        Note:
            Allowed fields are ``agent``, ``description``, ``instructions``,
            ``cron``, ``create_on_ping`` and ``permissions``. The configuration
            is rewritten and the scheduler is refreshed when running.

        :param name: task name to update.
        :type name: `str`
        :param changes: mapping of field name to new value.
        :type changes: `dict[str, Any]`
        :return: the refreshed job description, or ``None`` when ``name``
            does not match a configured task.
        :rtype: `dict[str, Any] | None`
        :raises ValueError: when ``changes`` is empty, contains an unsupported
            field, fails validation, or references an unknown agent role.
        """
        from con_pilot.conductor.models import TaskConfig

        unknown = [k for k in changes if k not in self._CRON_TASK_MUTABLE_FIELDS]
        if unknown:
            raise ValueError(f"Unsupported field(s): {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("No changes provided")

        cfg = self.config
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
        self._persist_config()
        self.reload_config()
        self._reschedule_after_config_change()
        return self._describe_cron_job(name)

    def remove_cron_job(self, name: str) -> bool:
        """
        Remove a task from configuration and unregister its scheduler job.

        Example:
            removed = pilot.remove_cron_job("nightly")

        :param name: name of the task to remove.
        :type name: `str`
        :return: ``True`` when the task existed and was removed, ``False``
            when no task with that name was found.
        :rtype: `bool`
        """
        cfg = self.config
        before = len(cfg.tasks)
        cfg.tasks = [t for t in cfg.tasks if t.name != name]
        if len(cfg.tasks) == before:
            return False

        self._persist_config()
        self.reload_config()

        with self._scheduler_lock:
            scheduler = self._scheduler
            if scheduler is not None and scheduler.running:
                try:
                    scheduler.remove_job(self._scheduler_job_id(name))
                except Exception as exc:  # noqa: BLE001
                    log.debug("remove_job(%s) failed: %s", name, exc)
        self._reschedule_after_config_change()
        return True

    def _cron_state_path(
        self, role: str, job_name: str, project: str | None = None
    ) -> str:
        root = self._role_cron_root(role, project)
        return os.path.join(root, ".state", f"{role}__{job_name}.last_run")

    def _last_run(
        self, role: str, job_name: str, project: str | None = None
    ) -> datetime:
        path = self._cron_state_path(role, job_name, project)
        if not os.path.exists(path):
            return datetime.fromtimestamp(0, tz=UTC)
        with open(path) as f:
            return datetime.fromisoformat(f.read().strip())

    def _save_last_run(
        self, role: str, job_name: str, dt: datetime, project: str | None = None
    ) -> None:
        state_dir = os.path.join(self._role_cron_root(role, project), ".state")
        os.makedirs(state_dir, exist_ok=True)
        with open(self._cron_state_path(role, job_name, project), "w") as f:
            f.write(dt.isoformat())

    def _cron_is_due(self, schedule: str, last_run: datetime) -> bool:
        now = datetime.now(tz=UTC)
        cron = croniter(schedule, last_run.replace(tzinfo=None))
        next_run = cron.get_next(datetime).replace(tzinfo=UTC)
        return now >= next_run

    def _append_pending(
        self,
        role: str,
        agent_name: str,
        job_name: str,
        task: str,
        schedule: str,
        *,
        project: str | None = None,
    ) -> None:
        cron_dir = self._role_cron_root(role, project)
        os.makedirs(cron_dir, exist_ok=True)
        pending_log = os.path.join(cron_dir, "pending.log")
        now = datetime.now(tz=UTC).isoformat()
        with open(pending_log, "a") as f:
            f.write(
                f"[{now}] role={role} agent={agent_name} job={job_name} "
                f"schedule={schedule!r}\n  task: {task}\n\n"
            )
        # Wake the dispatcher (no-op if not running).
        dispatcher = getattr(self, "_dispatcher", None)
        if dispatcher is not None:
            dispatcher.notify()

    def _cron_sweep(self, project: str | None = None) -> None:
        """
        Walk every active role and queue any cron jobs that are due.

        Example:
            pilot.cron()

        Note:
            Reads each role's ``{role}.cron`` TOML file (creating a placeholder
            on first run), checks every declared ``[[job]]`` against its
            ``last_run`` state file and appends due entries to ``pending.log``.

        :param project: when provided, restricts the scan to project-scoped
            roles attached to this project.
        :type project: `str | None`
        :return: None
        :rtype: `None`
        """
        # Ensure system cron dirs exist
        os.makedirs(self.cron_dir, exist_ok=True)
        os.makedirs(self.cron_state_dir, exist_ok=True)

        for role, role_cfg in self.config.agent_dicts.items():
            cron_cfg_raw = role_cfg.get("cron")
            if not cron_cfg_raw:
                continue
            if not role_cfg.get("active", role == "conductor"):
                continue

            scope = role_cfg.get("scope", "system")
            role_project = project if scope == "project" else None
            cron_dir = self._role_cron_root(role, role_project)
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
                last = self._last_run(role, job_name, role_project)
                if self._cron_is_due(schedule, last):
                    now = datetime.now(tz=UTC)
                    self._append_pending(
                        role, agent_name, job_name, task, schedule, project=role_project
                    )
                    self._save_last_run(role, job_name, now, role_project)
                    log.info("Queued: [%s] %s — %s...", role, job_name, task[:60])
