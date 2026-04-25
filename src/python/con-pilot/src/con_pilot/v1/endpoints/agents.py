"""Agent-related endpoints."""

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from con_pilot.core.models import AgentDetailResponse, AgentListResponse

from con_pilot.conductor import ConPilot

router = APIRouter(tags=["agents"])


class AgentConfigModifyRequest(BaseModel):
    """Patch body for mutable agent-configuration fields."""

    name: str | None = Field(default=None)
    active: bool | None = Field(default=None)
    sidekick: bool | None = Field(default=None)
    augmenting: bool | None = Field(default=None)
    model: str | None = Field(default=None)
    description: str | None = Field(default=None)
    instructions: str | None = Field(default=None)


def get_pilot() -> ConPilot:
    """Dependency to get the ConPilot instance.

    This is set at app startup via app.state.pilot.
    """
    from con_pilot.v1.api import get_pilot as _get_pilot

    return _get_pilot()


def verify_admin_key(
    x_admin_key: str | None = Header(None),
    pilot: ConPilot = Depends(get_pilot),
) -> ConPilot:
    """Verify the admin key header matches the system key."""
    pilot = pilot or get_pilot()
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


@router.get("/agents")
def list_agents(
    project: str | None = None, pilot: ConPilot = Depends(get_pilot)
) -> AgentListResponse:
    """List all agents and their status."""
    pilot = pilot or get_pilot()
    return pilot.list_agents(project=project)


@router.get("/agents/config")
def list_agent_configs(
    pilot: ConPilot = Depends(get_pilot),
) -> dict[str, AgentDetailResponse]:
    """Return agent descriptions for all agents from the runtime singleton."""
    pilot = pilot or get_pilot()
    return pilot.list_agent_configs()


@router.get("/agents/config/{name}")
def get_agent_config(
    name: str, pilot: ConPilot = Depends(get_pilot)
) -> AgentDetailResponse:
    """Return agent description for one agent by role key or display name."""
    pilot = pilot or get_pilot()
    result = pilot.get_agent_config(name)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{name}' not found",
        )
    return result


@router.patch(
    "/agents/config/{name}",
    dependencies=[Depends(verify_admin_key)],
)
def modify_agent_config(
    name: str,
    body: AgentConfigModifyRequest,
    pilot: ConPilot = Depends(get_pilot),
) -> AgentDetailResponse:
    """Modify mutable configuration for one agent and persist conductor config."""
    pilot = pilot or get_pilot()
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No changes provided",
        )

    try:
        result = pilot.update_agent_config(name, changes)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{name}' not found",
        )
    return result


@router.get("/agents/{name}")
def get_agent(name: str, pilot: ConPilot = Depends(get_pilot)) -> AgentDetailResponse:
    """Return properties and assigned tasks for an agent by role key or display name."""
    pilot = pilot or get_pilot()
    result = pilot.get_agent(name)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{name}' not found",
        )
    return result
