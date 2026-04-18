"""
trust.py — Trust registry management for ConPilot.

Manages the trust.json file that tracks registered projects and their directories.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from con_pilot.paths import PathResolver

log = logging.getLogger(__name__)


class TrustRegistry:
    """
    Manages the trust relationship between conductor and registered projects.

    The trust registry (stored in .github/trust.json) maps project names to
    their directories. The special "conductor" entry always points to CONDUCTOR_HOME.
    """

    def __init__(self, paths: PathResolver) -> None:
        self._paths = paths

    def load(self) -> dict[str, str]:
        """
        Return the trust map from .github/trust.json.

        Always includes the "conductor" entry pointing to CONDUCTOR_HOME.
        """
        trust: dict[str, str] = {"conductor": self._paths.home}
        if os.path.exists(self._paths.trust_file):
            try:
                with open(self._paths.trust_file) as f:
                    trust.update(json.load(f))
            except Exception:
                log.warning("Failed to load trust file: %s", self._paths.trust_file)
        # Ensure conductor entry is always correct
        trust["conductor"] = self._paths.home
        return trust

    def register(self, name: str, directory: str) -> None:
        """
        Add or update a project entry in .github/trust.json.

        Also re-exports TRUSTED_DIRECTORIES environment variable.
        """
        trust = self.load()
        if trust.get(name) == directory:
            return
        trust[name] = directory
        os.makedirs(os.path.dirname(self._paths.trust_file), exist_ok=True)
        with open(self._paths.trust_file, "w") as f:
            json.dump(trust, f, indent=2)
        self._update_env(trust)

    def unregister(self, name: str) -> None:
        """
        Remove a project entry from .github/trust.json.

        The "conductor" entry cannot be removed.
        """
        if name == "conductor":
            log.warning("Cannot unregister the conductor entry")
            return
        trust = self.load()
        if name not in trust:
            return
        del trust[name]
        with open(self._paths.trust_file, "w") as f:
            json.dump(trust, f, indent=2)
        self._update_env(trust)

    def get_directory(self, name: str) -> str | None:
        """Return the directory for a registered project, or None if not found."""
        return self.load().get(name)

    def list_projects(self) -> list[str]:
        """Return a list of registered project names (excluding 'conductor')."""
        trust = self.load()
        return [name for name in trust if name != "conductor"]

    def trusted_directories(self) -> list[str]:
        """Return a deduplicated list of all trusted directories."""
        trust = self.load()
        return list(dict.fromkeys(trust.values()))

    def _update_env(self, trust: dict[str, str]) -> None:
        """Update TRUSTED_DIRECTORIES environment variable."""
        dirs = list(dict.fromkeys(trust.values()))
        os.environ["TRUSTED_DIRECTORIES"] = ":".join(dirs)
