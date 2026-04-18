"""Application settings and configuration."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from con_pilot.paths import PathResolver


class Settings:
    """Application-wide settings derived from environment and paths."""

    DEFAULT_INTERVAL: int = 900  # 15 minutes
    DEFAULT_HOST: str = "127.0.0.1"
    DEFAULT_PORT: int = 8765

    def __init__(self, paths: "PathResolver | None" = None) -> None:
        self._paths = paths

    @property
    def conductor_home(self) -> str:
        """Return CONDUCTOR_HOME (env var or default ~/.conductor)."""
        return os.environ.get("CONDUCTOR_HOME", os.path.expanduser("~/.conductor"))

    @property
    def host(self) -> str:
        """Return the service host from env or default."""
        return os.environ.get("CON_PILOT_HOST", self.DEFAULT_HOST)

    @property
    def port(self) -> int:
        """Return the service port from env or default."""
        return int(os.environ.get("CON_PILOT_PORT", self.DEFAULT_PORT))

    @property
    def interval(self) -> int:
        """Return the sync interval in seconds."""
        return int(os.environ.get("CON_PILOT_INTERVAL", self.DEFAULT_INTERVAL))


@lru_cache
def get_settings() -> Settings:
    """Return cached settings instance."""
    return Settings()
