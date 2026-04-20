"""Health check endpoint."""

from importlib.metadata import version

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict:
    """Return service health status."""
    return {"status": "ok"}


@router.get("/version")
def get_version() -> dict:
    """Return service version."""
    try:
        v = version("con-pilot")
    except Exception:
        v = "unknown"
    return {"version": v}
