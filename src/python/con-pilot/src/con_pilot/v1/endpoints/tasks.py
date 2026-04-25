"""Task management endpoints.

Manages every entry under ``conductor.yaml`` ``tasks:``, regardless of whether
the task has a cron expression. Scheduled-only views remain available under
``/cron/jobs`` for back-compatibility.

- ``GET    /tasks``                List tasks (filter via ``?agent=`` / ``?scheduled=``).
- ``GET    /tasks/{name}``         Show one task.
- ``POST   /tasks``                Register a new task (admin).
- ``PATCH  /tasks/{name}``         Modify an existing task (admin).
- ``DELETE /tasks/{name}``         Remove a task (admin).
- ``POST   /tasks/{name}/run``     Manually queue a task into ``pending.log`` (admin).
"""

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from con_pilot.conductor import ConPilot

router = APIRouter(prefix="/tasks", tags=["tasks"])


def get_pilot() -> ConPilot:
    """Dependency to get the ConPilot instance."""
    from con_pilot.v1.api import get_pilot as _get_pilot

    return _get_pilot()


def verify_admin_key(
    x_admin_key: str | None = Header(None),
    pilot: ConPilot = Depends(get_pilot),
) -> ConPilot:
    """Verify the admin key header matches the system key."""
    if not x_admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin key required. Provide X-Admin-Key header.",
        )
    expected_key = pilot._load_or_generate_key()
    if x_admin_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin key",
        )
    return pilot


# ── Request/Response models ────────────────────────────────────────────────────


class TaskResponse(BaseModel):
    name: str
    agent: str
    description: str
    instructions: str
    cron: str | None = None
    create_on_ping: bool = False
    permissions: list[str] | None = None
    scheduled: bool
    registered: bool
    next_run_time: str | None = None
    job_id: str | None = None


class TaskListResponse(BaseModel):
    tasks: list[TaskResponse]


class TaskCreateRequest(BaseModel):
    name: str = Field(..., description="Unique task name")
    agent: str = Field(..., description="Agent role that will execute the task")
    description: str = Field(..., description="Human-readable task description")
    instructions: str = Field(
        ..., description="Detailed instructions for the executing agent"
    )
    cron: str | None = Field(
        default=None,
        description="Cron expression. Omit for manual-only tasks.",
    )
    create_on_ping: bool = Field(default=False)
    permissions: list[str] | None = Field(default=None)


class TaskModifyRequest(BaseModel):
    agent: str | None = None
    description: str | None = None
    instructions: str | None = None
    cron: str | None = None
    create_on_ping: bool | None = None
    permissions: list[str] | None = None


class TaskRunResponse(BaseModel):
    queued: bool
    task: str
    detail: str


# ── Routes ─────────────────────────────────────────────────────────────────────


def _filter_tasks(
    jobs: list[dict[str, Any]],
    *,
    agent: str | None,
    scheduled: bool | None,
) -> list[dict[str, Any]]:
    out = jobs
    if agent is not None:
        out = [j for j in out if j["agent"] == agent]
    if scheduled is not None:
        out = [j for j in out if bool(j["scheduled"]) is scheduled]
    return out


@router.get("", response_model=TaskListResponse)
def list_tasks(
    agent: str | None = None,
    scheduled: bool | None = None,
    pilot: ConPilot = Depends(get_pilot),
) -> TaskListResponse:
    """List every configured task, optionally filtered by agent or scheduled flag."""
    jobs = _filter_tasks(pilot.list_cron_jobs(), agent=agent, scheduled=scheduled)
    return TaskListResponse(tasks=[TaskResponse(**job) for job in jobs])


@router.get("/{name}", response_model=TaskResponse)
def get_task(name: str, pilot: ConPilot = Depends(get_pilot)) -> TaskResponse:
    """Return a single task by name."""
    job = pilot.get_cron_job(name)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{name}' not found",
        )
    return TaskResponse(**job)


@router.post(
    "",
    response_model=TaskResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin_key)],
)
def create_task(
    body: TaskCreateRequest, pilot: ConPilot = Depends(get_pilot)
) -> TaskResponse:
    """Register a new task (with optional cron) and refresh the scheduler."""
    payload: dict[str, Any] = body.model_dump(exclude_none=True)
    try:
        result = pilot.add_cron_job(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return TaskResponse(**result)


@router.patch(
    "/{name}",
    response_model=TaskResponse,
    dependencies=[Depends(verify_admin_key)],
)
def modify_task(
    name: str,
    body: TaskModifyRequest,
    pilot: ConPilot = Depends(get_pilot),
) -> TaskResponse:
    """Modify mutable fields on an existing task."""
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No changes provided",
        )
    try:
        result = pilot.update_cron_job(name, changes)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{name}' not found",
        )
    return TaskResponse(**result)


@router.delete(
    "/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_admin_key)],
)
def delete_task(name: str, pilot: ConPilot = Depends(get_pilot)) -> None:
    """Remove a task and unregister any associated APScheduler job."""
    if not pilot.remove_cron_job(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{name}' not found",
        )


@router.post(
    "/{name}/run",
    response_model=TaskRunResponse,
    dependencies=[Depends(verify_admin_key)],
)
def run_task(name: str, pilot: ConPilot = Depends(get_pilot)) -> TaskRunResponse:
    """Manually queue a task into the cron pending.log."""
    if pilot.get_cron_job(name) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Task '{name}' not found",
        )
    queued = pilot.run_task(name)
    if not queued:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Task '{name}' could not be queued (agent inactive, unknown role, "
                "or PROJECT_NAME unset for project-scoped agent)."
            ),
        )
    return TaskRunResponse(
        queued=True, task=name, detail=f"Task '{name}' queued in pending.log."
    )


# ── Dispatcher ─────────────────────────────────────────────────────────────────


class DispatcherStatusResponse(BaseModel):
    running: bool
    processed: int
    failed: int
    skipped: int
    last_run: str | None = None
    last_error: str | None = None
    pending_offset: int
    pending_size: int


class DispatcherDrainResponse(BaseModel):
    processed: int
    failed: int
    skipped: int


def _get_dispatcher(pilot: ConPilot) -> Any:
    return getattr(pilot, "_dispatcher", None)


@router.get("/dispatcher/status", response_model=DispatcherStatusResponse)
def dispatcher_status(
    pilot: ConPilot = Depends(get_pilot),
) -> DispatcherStatusResponse:
    """Return current state of the pending-task dispatcher."""
    dispatcher = _get_dispatcher(pilot)
    if dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dispatcher is not running",
        )
    return DispatcherStatusResponse(**dispatcher.status())


@router.post(
    "/dispatcher/drain",
    response_model=DispatcherDrainResponse,
    dependencies=[Depends(verify_admin_key)],
)
async def dispatcher_drain(
    pilot: ConPilot = Depends(get_pilot),
) -> DispatcherDrainResponse:
    """Force one drain pass of the pending dispatcher."""
    dispatcher = _get_dispatcher(pilot)
    if dispatcher is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dispatcher is not running",
        )
    summary = await dispatcher.drain_once()
    return DispatcherDrainResponse(**summary)
