"""Health check endpoint."""

from importlib.metadata import version

from fastapi import APIRouter, Request

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


@router.get("/startup-proof")
def startup_proof(request: Request, session_id: SessionIdField = None) -> dict:
    """Return concrete startup proof for Copilot + SDK + conductor session wiring."""
    state = request.app.state
    service = getattr(state, "copilot_service", None)

    try:
        copilot_version = version("copilot")
    except Exception:
        copilot_version = None

    try:
        copilot_sdk_version = version("github-copilot-sdk")
    except Exception:
        copilot_sdk_version = None

    result: dict[str, object] = {
        "status": "ok",
        "copilot_package_version": copilot_version,
        "copilot_sdk_package_version": copilot_sdk_version,
        "copilot_service_present": service is not None,
        "copilot_sdk_import_available": bool(
            getattr(service, "is_available", False)
        ),
        "copilot_client_started": bool(getattr(service, "_client", None)),
        "conductor_session_started": bool(
            getattr(service, "_conductor_session", None)
        ),
        "copilot_startup_complete": bool(
            getattr(state, "copilot_startup_complete", False)
        ),
        "copilot_startup_error": getattr(state, "copilot_startup_error", None),
    }
    if session_id is not None:
        result["session_id"] = session_id
    return result
