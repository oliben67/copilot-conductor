"""FastAPI application factory.

Wires per-domain routers (agents, cron, tasks, projects, etc.) under a
configurable ``/{base}/{version}`` prefix and manages the application
lifespan (Copilot SDK service, APScheduler, pending dispatcher, sync
thread).
"""

import os
import threading
import time
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from con_pilot.logger import app_logger

from con_pilot.agents.router import router as agents_router
from con_pilot.auth.router import router as login_router
from con_pilot.configs.router import router as config_router
from con_pilot.configs.validation import router as validation_router
from con_pilot.cron.router import router as cron_router
from con_pilot.health.router import router as health_router
from con_pilot.projects.router import router as projects_router
from con_pilot.snapshots.router import router as snapshot_router
from con_pilot.conductor.router import router as conductor_router
from con_pilot.documents.router import router as documents_router
from con_pilot.sync.router import router as sync_router
from con_pilot.tasks.router import router as tasks_router
from con_pilot.users.router import router as users_router

from con_pilot.conductor import ConPilot

log = app_logger.bind(module=__name__)

# Global reference to the ConPilot instance (set at app creation)
_pilot: ConPilot | None = None

# Main router combining all endpoint routers
router = APIRouter()
router.include_router(health_router)
router.include_router(login_router)
router.include_router(users_router)
router.include_router(conductor_router)
router.include_router(agents_router)
router.include_router(config_router)
router.include_router(snapshot_router)
router.include_router(sync_router)
router.include_router(cron_router)
router.include_router(tasks_router)
router.include_router(projects_router)
router.include_router(validation_router)
router.include_router(documents_router)


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
        app.state.copilot_service = None
        app.state.copilot_startup_complete = False
        app.state.copilot_startup_error = None
        app.state.scheduler_startup_complete = False
        app.state.scheduler_startup_error = None

        # Initialize documents DB
        import os
        from con_pilot.documents.db import init_db as _init_documents_db
        from con_pilot.documents.worker import init_worker as _init_document_worker
        _docs_db_path = os.path.join(pilot.home, "documents.sqlite3")
        _init_documents_db(_docs_db_path)
        _document_worker = _init_document_worker(_docs_db_path)
        await _document_worker.start()

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
        except Exception as e:  # noqa: BLE001
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
        pilot.ensure_system_agents()
        log.debug("System agent files ensured")

        # Start Copilot SDK service so conductor session exists at startup.
        try:
            from con_pilot.conductor.copilot_service import CopilotAgentService

            copilot_service = CopilotAgentService(pilot)
            app.state.copilot_service = copilot_service
            log.debug("Starting CopilotAgentService")
            await copilot_service.start()
            app.state.copilot_startup_complete = bool(
                getattr(copilot_service, "_client", None)
                and getattr(copilot_service, "conductor_session", None)
            )
            if app.state.copilot_startup_complete:
                log.info("CopilotAgentService startup complete")
            else:
                app.state.copilot_startup_error = (
                    "CopilotAgentService initialized but no active conductor session was "
                    "created. Check GitHub token and SDK availability."
                )
                log.warning(app.state.copilot_startup_error)
        except Exception:
            app.state.copilot_startup_error = "CopilotAgentService startup failed"
            log.exception("CopilotAgentService startup failed")

        # Start APScheduler cron service backed by SQLite under CONDUCTOR_HOME.
        try:
            await pilot.cron.start_scheduler()
            app.state.scheduler_startup_complete = True
        except Exception:
            app.state.scheduler_startup_error = "APScheduler startup failed"
            log.exception("APScheduler startup failed")

        # Start pending-task dispatcher (drains pending.log into the conductor session).
        app.state.dispatcher = None
        if copilot_service is not None:
            try:
                from con_pilot.cron.dispatch import PendingDispatcher

                dispatcher = PendingDispatcher(pilot, copilot_service)
                pilot._dispatcher = dispatcher
                app.state.dispatcher = dispatcher
                await dispatcher.start()
                log.info("PendingDispatcher started")
            except Exception:
                log.exception("PendingDispatcher startup failed")

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

        try:
            await _document_worker.stop()
        except Exception:
            log.exception("DocumentWorker shutdown failed")

        if app.state.dispatcher is not None:
            try:
                await app.state.dispatcher.stop()
                log.info("PendingDispatcher stopped")
            except Exception:
                log.exception("PendingDispatcher shutdown failed")
            pilot._dispatcher = None

        if copilot_service is not None:
            try:
                log.debug("Stopping CopilotAgentService")
                await copilot_service.stop()
                log.info("CopilotAgentService stopped")
            except Exception:
                log.exception("CopilotAgentService shutdown failed")

        try:
            await pilot.cron.stop_scheduler()
        except Exception:
            log.exception("APScheduler shutdown failed")

    app = FastAPI(title="con-pilot", lifespan=lifespan)
    app.include_router(router, prefix=_api_prefix())

    return app
