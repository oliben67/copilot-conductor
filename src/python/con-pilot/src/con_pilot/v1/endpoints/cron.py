"""Cron job management endpoints.

Exposes the scheduled-task catalogue backed by APScheduler:

- ``GET    /cron/jobs``             List all configured tasks and their scheduler state.
- ``GET    /cron/jobs/{name}``      Show one task.
- ``POST   /cron/jobs``             Register a new task (persisted to conductor config).
- ``PATCH  /cron/jobs/{name}``      Modify an existing task.
- ``DELETE /cron/jobs/{name}``      Remove a task.
- ``GET    /cron/logs``             Tail the cron pending.log.

Mutating endpoints require the admin key (``X-Admin-Key`` header) — same
contract as ``/agents/config``.
"""

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from con_pilot.conductor import ConPilot

router = APIRouter(prefix="/cron", tags=["cron"])


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


class CronJobResponse(BaseModel):
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


class CronJobListResponse(BaseModel):
    jobs: list[CronJobResponse]


class CronJobCreateRequest(BaseModel):
    name: str = Field(..., description="Unique task name")
    agent: str = Field(..., description="Agent role that will execute the task")
    description: str = Field(..., description="Human-readable task description")
    instructions: str = Field(
        ..., description="Detailed instructions for the executing agent"
    )
    cron: str | None = Field(
        default=None,
        description="Cron expression (5 or 6 fields). Omit for manual-only tasks.",
    )
    create_on_ping: bool = Field(default=False)
    permissions: list[str] | None = Field(default=None)


class CronJobModifyRequest(BaseModel):
    agent: str | None = None
    description: str | None = None
    instructions: str | None = None
    cron: str | None = None
    create_on_ping: bool | None = None
    permissions: list[str] | None = None


class CronLogResponse(BaseModel):
    path: str
    total: int
    lines: list[str]


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("/jobs", response_model=CronJobListResponse)
def list_cron_jobs(pilot: ConPilot = Depends(get_pilot)) -> CronJobListResponse:
    """List every configured task with its scheduler state."""
    jobs = [CronJobResponse(**job) for job in pilot.list_cron_jobs()]
    return CronJobListResponse(jobs=jobs)


@router.get("/jobs/{name}", response_model=CronJobResponse)
def get_cron_job(
    name: str, pilot: ConPilot = Depends(get_pilot)
) -> CronJobResponse:
    """Return a single task by name."""
    job = pilot.get_cron_job(name)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cron job '{name}' not found",
        )
    return CronJobResponse(**job)


@router.post(
    "/jobs",
    response_model=CronJobResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_admin_key)],
)
def create_cron_job(
    body: CronJobCreateRequest, pilot: ConPilot = Depends(get_pilot)
) -> CronJobResponse:
    """Register a new task (with optional cron) and refresh the scheduler."""
    payload: dict[str, Any] = body.model_dump(exclude_none=True)
    try:
        result = pilot.add_cron_job(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return CronJobResponse(**result)


@router.patch(
    "/jobs/{name}",
    response_model=CronJobResponse,
    dependencies=[Depends(verify_admin_key)],
)
def modify_cron_job(
    name: str,
    body: CronJobModifyRequest,
    pilot: ConPilot = Depends(get_pilot),
) -> CronJobResponse:
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
            detail=f"Cron job '{name}' not found",
        )
    return CronJobResponse(**result)


@router.delete(
    "/jobs/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_admin_key)],
)
def delete_cron_job(name: str, pilot: ConPilot = Depends(get_pilot)) -> None:
    """Remove a task and unregister its APScheduler job."""
    removed = pilot.remove_cron_job(name)
    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Cron job '{name}' not found",
        )


@router.get("/logs", response_model=CronLogResponse)
def get_cron_logs(
    lines: int | None = None,
    project: str | None = None,
    pilot: ConPilot = Depends(get_pilot),
) -> CronLogResponse:
    """Return the (optionally-trimmed) contents of the cron pending.log."""
    return CronLogResponse(**pilot.read_cron_logs(lines=lines, project=project))
