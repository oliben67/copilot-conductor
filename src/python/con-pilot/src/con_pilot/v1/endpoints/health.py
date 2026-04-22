"""Health check endpoint."""

from importlib.metadata import version

from fastapi import APIRouter

from con_pilot.session_id import SessionIdField

router = APIRouter(tags=["health"])


@router.get("/health")
def health(session_id: SessionIdField = None) -> dict:
    """Return service health status."""
    result: dict = {"status": "ok"}
    if session_id is not None:
        result["session_id"] = session_id
    return result


@router.get("/version")
def get_version() -> dict:
    """Return service version."""
    try:
        v = version("con-pilot")
    except Exception:
        v = "unknown"
    return {"version": v}
