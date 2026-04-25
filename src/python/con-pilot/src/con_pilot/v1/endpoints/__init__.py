"""v1 API endpoints module."""

from con_pilot.v1.endpoints.agents import router as agents_router
from con_pilot.v1.endpoints.config import router as config_router
from con_pilot.v1.endpoints.cron import router as cron_router
from con_pilot.v1.endpoints.health import router as health_router
from con_pilot.v1.endpoints.login import router as login_router
from con_pilot.v1.endpoints.projects import router as projects_router
from con_pilot.v1.endpoints.snapshot import router as snapshot_router
from con_pilot.v1.endpoints.sync import router as sync_router
from con_pilot.v1.endpoints.tasks import router as tasks_router
from con_pilot.v1.endpoints.users import router as users_router
from con_pilot.v1.endpoints.validation import router as validation_router

__all__ = [
    "agents_router",
    "config_router",
    "cron_router",
    "health_router",
    "login_router",
    "projects_router",
    "snapshot_router",
    "sync_router",
    "tasks_router",
    "users_router",
    "validation_router",
]
