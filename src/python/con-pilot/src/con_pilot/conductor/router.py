"""Conductor service endpoints."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter(tags=["conductor"])


class SessionInfo(BaseModel):
    role: str
    session_id: str | None
    agent_name: str | None


class RegisteredSessionsResponse(BaseModel):
    conductor: SessionInfo | None
    registered: list[SessionInfo]


@router.get("/conductor/sessions", response_model=RegisteredSessionsResponse)
def list_sessions(request: Request) -> RegisteredSessionsResponse:
    """Return the conductor session and all registered agent sessions."""
    service = getattr(request.app.state, "copilot_service", None)

    conductor: SessionInfo | None = None
    registered: list[SessionInfo] = []

    if service is not None:
        cs = getattr(service, "conductor_session", None)
        if cs is not None:
            conductor = SessionInfo(
                role="conductor",
                session_id=getattr(cs, "session_id", None),
                agent_name=getattr(cs, "agent", None),
            )

        for role_key, session in service.registered_sessions.items():
            role, _, agent_name = role_key.partition(":")
            registered.append(
                SessionInfo(
                    role=role,
                    session_id=getattr(session, "session_id", None),
                    agent_name=agent_name or None,
                )
            )

    return RegisteredSessionsResponse(conductor=conductor, registered=registered)
