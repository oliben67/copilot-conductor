"""
paths.py — Centralized path resolution for ConPilot.

All filesystem paths derived from CONDUCTOR_HOME are computed here.
"""

import os
from pathlib import Path


class PathResolver:
    """
    Resolves all paths relative to CONDUCTOR_HOME.

    Instantiating ``PathResolver`` sets ``CONDUCTOR_HOME`` in the process
    environment so all child processes inherit it.

    Parameters
    ----------
    conductor_home:
        Override the conductor home directory. When omitted, resolved via
        (in order) the ``CONDUCTOR_HOME`` env var and self-location from
        the package path.
    """

    def __init__(self, conductor_home: str | None = None) -> None:
        self.home: str = self._resolve_home(conductor_home)

    @staticmethod
    def _resolve_home(conductor_home: str | None = None) -> str:
        """
        Resolve CONDUCTOR_HOME and write it into ``os.environ``.

        Priority: explicit arg → ``CONDUCTOR_HOME`` env var → self-location
        (this file lives at ``$CONDUCTOR_HOME/python/con-pilot/src/con_pilot/``).
        """
        home = conductor_home or os.environ.get("CONDUCTOR_HOME", "")
        if not home:
            candidate = Path(__file__).parents[4]
            # Check for either conductor.yaml or conductor.json
            if (candidate / "conductor.yaml").exists() or (
                candidate / "conductor.json"
            ).exists():
                home = str(candidate)
        if home:
            os.environ["CONDUCTOR_HOME"] = home
        return home

    # ── Core paths ─────────────────────────────────────────────────────────────

    @property
    def config_path(self) -> str:
        """
        Path to conductor config file.

        Prefers conductor.yaml if it exists, otherwise falls back to conductor.json.
        Returns the YAML path even if neither exists (for new installations).
        """
        yaml_path = os.path.join(self.home, "conductor.yaml")
        json_path = os.path.join(self.home, "conductor.json")
        if os.path.exists(yaml_path):
            return yaml_path
        if os.path.exists(json_path):
            return json_path
        # Default to YAML for new installations
        return yaml_path

    @property
    def config_yaml_path(self) -> str:
        """Path to conductor.yaml."""
        return os.path.join(self.home, "conductor.yaml")

    @property
    def github_dir(self) -> str:
        """Path to .github directory."""
        return os.path.join(self.home, ".github")

    @property
    def instructions_dir(self) -> str:
        """Path to .instructions directory for snapshots and backups."""
        return os.path.join(self.home, ".instructions")

    @property
    def trust_file(self) -> str:
        """Path to .github/trust.json."""
        return os.path.join(self.github_dir, "trust.json")

    # ── Agent paths ────────────────────────────────────────────────────────────

    @property
    def agents_dir(self) -> str:
        """Path to .github/agents directory."""
        return os.path.join(self.github_dir, "agents")

    @property
    def retired_dir(self) -> str:
        """Path to .github/agents/retired directory."""
        return os.path.join(self.agents_dir, "retired")

    @property
    def templates_dir(self) -> str:
        """Path to .github/agents/templates directory."""
        return os.path.join(self.agents_dir, "templates")

    # ── Cron paths ─────────────────────────────────────────────────────────────

    @property
    def cron_dir(self) -> str:
        """Path to .github/agents/cron directory."""
        return os.path.join(self.agents_dir, "cron")

    @property
    def cron_state_dir(self) -> str:
        """Path to .github/agents/cron/.state directory."""
        return os.path.join(self.cron_dir, ".state")

    @property
    def pending_log(self) -> str:
        """Path to .github/agents/cron/pending.log."""
        return os.path.join(self.cron_dir, "pending.log")

    # ── Project paths ──────────────────────────────────────────────────────────

    @property
    def projects_dir(self) -> str:
        """Path to .github/projects directory."""
        return os.path.join(self.github_dir, "projects")

    def project_dir(self, project: str) -> str:
        """Path to .github/projects/<project> directory."""
        return os.path.join(self.projects_dir, project)

    def project_agents_dir(self, project: str) -> str:
        """Path to .github/projects/<project>/agents directory."""
        return os.path.join(self.project_dir(project), "agents")

    def project_retired_dir(self, project: str) -> str:
        """Path to .github/projects/<project>/agents/retired directory."""
        return os.path.join(self.project_agents_dir(project), "retired")

    def project_cron_dir(self, project: str) -> str:
        """Path to .github/projects/<project>/cron directory."""
        return os.path.join(self.project_dir(project), "cron")

    # ── Logging paths ──────────────────────────────────────────────────────────

    @property
    def sync_log(self) -> str:
        """Path to .github/scripts/sync_agents.log."""
        return os.path.join(self.github_dir, "scripts", "sync_agents.log")

    # ── Security paths ─────────────────────────────────────────────────────────

    @property
    def key_file(self) -> str:
        """
        Path to the system key file.

        Prefers XDG_DATA_HOME (flatpak sandbox) for new installs,
        falls back to legacy location if it exists.
        """
        xdg_data = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        flatpak_key = os.path.join(xdg_data, "key")
        if os.path.exists(flatpak_key):
            return flatpak_key
        # Fall back to legacy location
        legacy_key = os.path.join(self.home, "python", "con-pilot", "key")
        if os.path.exists(legacy_key):
            return legacy_key
        # Default to flatpak location for new keys
        return flatpak_key

    # ── Schema path ────────────────────────────────────────────────────────────

    @property
    def schema_path(self) -> str:
        """
        Path to conductor.schema.json.

        Checks (in order):
        1. $CONDUCTOR_HOME/src/schemas/conductor.schema.json
        2. $CONDUCTOR_HOME/schemas/conductor.schema.json
        3. Package-relative path
        """
        candidates = [
            os.path.join(self.home, "src", "schemas", "conductor.schema.json"),
            os.path.join(self.home, "schemas", "conductor.schema.json"),
            os.path.join(Path(__file__).parents[4], "schemas", "conductor.schema.json"),
        ]
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return candidates[0]  # Return default even if not found
