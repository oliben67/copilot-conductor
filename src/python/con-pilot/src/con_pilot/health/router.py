"""Health check endpoint."""

import os
from importlib.metadata import version

from fastapi import APIRouter, Request

from con_pilot.auth.schemas import SessionIdField

router = APIRouter(tags=["health"])

_DEV_NOTICE = "DEV BUILD — not for production use."


def _resolve_version() -> str:
    """Resolve the con-pilot package version.

    Dev builds carry a ``-dev`` suffix because:

    * ``task build-dev`` writes ``X.Y.Z-dev`` into the VERSION files (which
      drives release filenames);
    * the bundled AppRun exports ``CONDUCTOR_ENV=DEV`` from the AppDir ``dev``
      marker, and the runtime appends ``-dev`` here so ``importlib.metadata``
      (which serves the wheel's PEP 440 version) does not need to be polluted
      with PEP-440-normalized ``.dev0`` segments.
    """
    try:
        v = version("con-pilot")
    except Exception:  # noqa: BLE001
        v = "unknown"
    if os.environ.get("CONDUCTOR_ENV") == "DEV" and not _is_dev_version(v):
        v = f"{v}-dev"
    return v


def _is_dev_version(v: str) -> bool:
    """Return True when ``v`` carries any PEP 440 / informal dev suffix."""
    if not v:
        return False
    return v.endswith("-dev") or v.endswith(".dev") or ".dev" in v or "+dev" in v


@router.get("/health")
def health(session_id: SessionIdField = None) -> dict:
    """Return service health status."""
    v = _resolve_version()
    result: dict = {"status": "ok", "version": v}
    if _is_dev_version(v):
        result["dev_build"] = True
        result["notice"] = _DEV_NOTICE
    if session_id is not None:
        result["session_id"] = session_id
    return result


@router.get("/version")
def get_version() -> dict:
    """Return service version. Dev builds carry a ``-dev`` suffix and a notice."""
    v = _resolve_version()
    result: dict = {"version": v}
    if _is_dev_version(v):
        result["dev_build"] = True
        result["notice"] = _DEV_NOTICE
    return result


@router.get("/startup-proof")
def startup_proof(request: Request, session_id: SessionIdField = None) -> dict:
    """Return concrete startup proof for Copilot + SDK + conductor session wiring."""
    state = request.app.state
    service = getattr(state, "copilot_service", None)

    try:
        copilot_version = version("copilot")
    except Exception:  # noqa: BLE001
        copilot_version = None

    try:
        copilot_sdk_version = version("github-copilot-sdk")
    except Exception:  # noqa: BLE001
        copilot_sdk_version = None

    result: dict[str, object] = {
        "status": "ok",
        "copilot_package_version": copilot_version,
        "copilot_sdk_package_version": copilot_sdk_version,
        "copilot_service_present": service is not None,
        "copilot_sdk_import_available": bool(getattr(service, "is_available", False)),
        "copilot_client_started": bool(getattr(service, "_client", None)),
        "conductor_session_started": bool(getattr(service, "_conductor_session", None)),
        "copilot_startup_complete": bool(
            getattr(state, "copilot_startup_complete", False)
        ),
        "copilot_startup_error": getattr(state, "copilot_startup_error", None),
    }
    if session_id is not None:
        result["session_id"] = session_id
    return result
