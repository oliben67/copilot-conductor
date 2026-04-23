"""API v1 router configuration.

This module sets up the main APIRouter for v1 and provides
the ConPilot dependency injection.
"""

import os
import threading
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import APIRouter, FastAPI

from con_pilot.logger import app_logger

from con_pilot.v1.endpoints import (
    agents_router,
    config_router,
    health_router,
    login_router,
    projects_router,
    snapshot_router,
    sync_router,
    users_router,
    validation_router,
)

if TYPE_CHECKING:
    from con_pilot.conductor import ConPilot

log = app_logger.bind(module=__name__)

# Global reference to the ConPilot instance (set at app creation)
_pilot: ConPilot | None = None

# Main router combining all endpoint routers
router = APIRouter()
router.include_router(health_router)
router.include_router(login_router)
router.include_router(users_router)
router.include_router(agents_router)
router.include_router(config_router)
router.include_router(snapshot_router)
router.include_router(sync_router)
router.include_router(projects_router)
router.include_router(validation_router)


def _normalize_segment(value: str) -> str:
    return value.strip().strip("/")


def _api_prefix() -> str:
    base = _normalize_segment(os.environ.get("CON_PILOT_API_BASE", "api"))
    version = _normalize_segment(os.environ.get("CON_PILOT_API_VERSION", "v1"))
    return f"/{base}/{version}"


def get_pilot() -> ConPilot:
    """Get the ConPilot instance for dependency injection.

    Raises:
        RuntimeError: If the pilot has not been initialized.
    """
    if _pilot is None:
        raise RuntimeError("ConPilot instance not initialized. Call create_app first.")
    return _pilot


def set_pilot(pilot: ConPilot) -> None:
    """Set the global ConPilot instance."""
    global _pilot
    _pilot = pilot


def create_app(pilot: ConPilot, interval: int | None = None) -> FastAPI:
    """Create and configure the FastAPI application.
    Parameters
    ----------
    pilot:
        The ConPilot instance to use for all operations.
    interval:
        Sync interval in seconds. Defaults to pilot.DEFAULT_INTERVAL.
    Returns
    -------
    FastAPI:
        The configured FastAPI application.
    """

    set_pilot(pilot)
    cycle = interval if interval is not None else pilot.DEFAULT_INTERVAL

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        copilot_service = None

        # Initialize config store and load all versions
        pilot.config_store.ensure_scores_dir()
        pilot.config_store.load_all()
        log.info(
            "Loaded %d config versions from %s",
            len(pilot.config_store.versions),
            pilot.config_store.scores_dir,
        )

        # Backup current active config if it has version info
        try:
            backup = pilot.config_store.backup_active()
            if backup:
                log.info("Backed up active config: version %s", backup.version)
        except Exception as e:
            log.warning("Failed to backup active config: %s", e)

        # Initialize snapshot service and load index
        pilot.snapshot_service.ensure_instructions_dir()
        pilot.snapshot_service._load_index()
        log.info(
            "Loaded %d snapshots from %s",
            len(pilot.snapshot_service.versions),
            pilot.snapshot_service.instructions_dir,
        )

        # Start snapshot watcher (check for changes every 60 seconds)
        pilot.snapshot_service.start_watcher(interval=60)

        log.debug("Ensuring system agent files exist")
        pilot._ensure_system_agents()
        log.debug("System agent files ensured")

        # Start Copilot SDK service so conductor session exists at startup.
        try:
            from con_pilot.core.services.copilot_service import CopilotAgentService  # noqa: PLC0415

            copilot_service = CopilotAgentService(pilot)
            app.state.copilot_service = copilot_service
            log.debug("Starting CopilotAgentService")
            await copilot_service.start()
            log.info("CopilotAgentService startup complete")
        except Exception:
            log.exception("CopilotAgentService startup failed")

        def _loop() -> None:
            while True:
                try:
                    pilot.sync()
                except Exception:
                    log.exception("Sync cycle failed")
                log.info("Next sync in %ds", cycle)
                time.sleep(cycle)

        t = threading.Thread(target=_loop, daemon=True)
        t.start()
        log.info("con-pilot background sync started (interval=%ds)", cycle)
        yield

        # Cleanup: stop snapshot watcher
        pilot.snapshot_service.stop_watcher()

        if copilot_service is not None:
            try:
                log.debug("Stopping CopilotAgentService")
                await copilot_service.stop()
                log.info("CopilotAgentService stopped")
            except Exception:
                log.exception("CopilotAgentService shutdown failed")

    app = FastAPI(title="con-pilot", lifespan=lifespan)
    app.include_router(router, prefix=_api_prefix())

    return app
