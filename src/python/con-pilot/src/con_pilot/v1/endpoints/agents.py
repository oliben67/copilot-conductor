"""Agent-related endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

from con_pilot.core.models import AgentListResponse

if TYPE_CHECKING:
    from con_pilot.conductor import ConPilot

router = APIRouter(tags=["agents"])


def get_pilot() -> "ConPilot":
    """Dependency to get the ConPilot instance.
    
    This is set at app startup via app.state.pilot.
    """
    from con_pilot.v1.api import get_pilot as _get_pilot

    return _get_pilot()


@router.get("/agents")
def list_agents(project: str | None = None, pilot: "ConPilot" = Depends(get_pilot)) -> AgentListResponse:
    """List all agents and their status."""
    return pilot.list_agents(project=project)
