"""v1 API endpoints module."""

from con_pilot.v1.endpoints.agents import router as agents_router
from con_pilot.v1.endpoints.config import router as config_router
from con_pilot.v1.endpoints.health import router as health_router
from con_pilot.v1.endpoints.projects import router as projects_router
from con_pilot.v1.endpoints.snapshot import router as snapshot_router
from con_pilot.v1.endpoints.sync import router as sync_router
from con_pilot.v1.endpoints.validation import router as validation_router

__all__ = [
    "agents_router",
    "config_router",
    "health_router",
    "projects_router",
    "snapshot_router",
    "sync_router",
    "validation_router",
]
