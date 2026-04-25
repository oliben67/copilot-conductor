"""
con-pilot — Conductor pilot library.

Provides agent sync and cron scheduling utilities for the conductor system.
``ConPilot`` is the single entry point for all functionality.
"""

from con_pilot.security.auth import (
    GitHubToken,
    resolve_github_token,
)
from con_pilot.conductor import ConPilot
from con_pilot.runtime.exceptions import TokenConflictError, TokenNotFoundError
from con_pilot.server import create_app

__all__ = [
    "ConPilot",
    "GitHubToken",
    "TokenConflictError",
    "TokenNotFoundError",
    "create_app",
    "resolve_github_token",
]
