"""
config_store.py — Configuration version management for con-pilot.

Provides in-memory caching and persistent storage of conductor configuration
versions in CONDUCTOR_HOME/.scores/.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, Field

from con_pilot.core.models.config import ConductorConfig, VersionConfig

if TYPE_CHECKING:
    from con_pilot.paths import PathResolver

log = logging.getLogger(__name__)


class ConfigVersion(BaseModel):
    """Metadata about a stored configuration version."""

    version: str = Field(..., description="Semantic version number")
    timestamp: datetime = Field(..., description="When this version was saved")
    file: str = Field(..., description="Filename in .scores/")
    description: str | None = Field(default=None, description="Version description")
    notes: str | None = Field(default=None, description="Additional notes")


class ConfigIndex(BaseModel):
    """Index of all stored configuration versions."""

    versions: list[ConfigVersion] = Field(default_factory=list)


class VersionExistsError(Exception):
    """Raised when attempting to create a config with an existing version number."""

    def __init__(self, existing: ConfigVersion) -> None:
        self.existing = existing
        super().__init__(
            f"Version {existing.version} already exists "
            f"(created: {existing.timestamp.isoformat()}, "
            f"description: {existing.description or 'n/a'})"
        )


class VersionNotFoundError(Exception):
    """Raised when a requested version does not exist."""

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(f"Version {version} not found")


class ConfigStore:
    """
    Manages conductor configuration versions.

    Provides:
    - In-memory cache of configs keyed by version number
    - Persistent storage in CONDUCTOR_HOME/.scores/
    - JSON index file tracking version metadata
    - Diff capabilities between versions
    """

    SCORES_DIR = ".scores"
    INDEX_FILE = "index.json"

    def __init__(self, paths: "PathResolver") -> None:
        self._paths = paths
        self._cache: dict[str, ConductorConfig] = {}
        self._index: ConfigIndex | None = None

    @property
    def scores_dir(self) -> str:
        """Path to CONDUCTOR_HOME/.scores/"""
        return os.path.join(self._paths.home, self.SCORES_DIR)

    @property
    def index_path(self) -> str:
        """Path to CONDUCTOR_HOME/.scores/index.json"""
        return os.path.join(self.scores_dir, self.INDEX_FILE)

    def _config_filename(self, version: str) -> str:
        """Generate filename for a config version: conductor.{version}.yaml"""
        return f"conductor.{version}.yaml"

    def _config_path(self, version: str) -> str:
        """Full path to a config version file."""
        return os.path.join(self.scores_dir, self._config_filename(version))

    # ── Initialization ─────────────────────────────────────────────────────────

    def ensure_scores_dir(self) -> None:
        """Create .scores directory if it doesn't exist."""
        os.makedirs(self.scores_dir, exist_ok=True)
        log.info("Ensured scores directory: %s", self.scores_dir)

    def load_index(self) -> ConfigIndex:
        """Load or create the version index."""
        if self._index is not None:
            return self._index

        if os.path.exists(self.index_path):
            try:
                with open(self.index_path) as f:
                    data = json.load(f)
                self._index = ConfigIndex(**data)
            except Exception as e:
                log.warning("Failed to load index, creating new: %s", e)
                self._index = ConfigIndex()
        else:
            self._index = ConfigIndex()

        return self._index

    def _save_index(self) -> None:
        """Persist the index to disk."""
        self.ensure_scores_dir()
        with open(self.index_path, "w") as f:
            json.dump(self._index.model_dump(mode="json"), f, indent=2, default=str)
        log.info("Saved config index: %s", self.index_path)

    # ── Cache Operations ───────────────────────────────────────────────────────

    def load_all(self) -> dict[str, ConductorConfig]:
        """
        Load all stored configs into memory.

        Returns the cache dict keyed by version number.
        """
        self.ensure_scores_dir()
        index = self.load_index()

        for version_meta in index.versions:
            if version_meta.version not in self._cache:
                path = self._config_path(version_meta.version)
                if os.path.exists(path):
                    try:
                        with open(path) as f:
                            data = yaml.safe_load(f)
                        self._cache[version_meta.version] = ConductorConfig(**data)
                    except Exception as e:
                        log.warning(
                            "Failed to load config %s: %s", version_meta.version, e
                        )

        log.info("Loaded %d config versions into memory", len(self._cache))
        return self._cache

    def get(self, version: str) -> ConductorConfig | None:
        """Get a config by version number from cache."""
        if version not in self._cache:
            # Try loading from disk
            path = self._config_path(version)
            if os.path.exists(path):
                with open(path) as f:
                    data = yaml.safe_load(f)
                self._cache[version] = ConductorConfig(**data)
        return self._cache.get(version)

    def get_or_raise(self, version: str) -> ConductorConfig:
        """Get a config by version number, raising if not found."""
        config = self.get(version)
        if config is None:
            raise VersionNotFoundError(version)
        return config

    @property
    def versions(self) -> list[ConfigVersion]:
        """List all stored versions with metadata."""
        return self.load_index().versions

    # ── Storage Operations ─────────────────────────────────────────────────────

    def save(
        self,
        config: ConductorConfig,
        *,
        allow_overwrite: bool = False,
    ) -> ConfigVersion:
        """
        Save a new configuration version.

        Args:
            config: The conductor config to save (must have version info).
            allow_overwrite: If True, allows updating an existing version.

        Returns:
            ConfigVersion metadata for the saved config.

        Raises:
            ValueError: If config has no version info.
            VersionExistsError: If version exists and allow_overwrite is False.
        """
        if not config.version:
            raise ValueError("Configuration must have version info to save")

        version_num = config.version.number
        index = self.load_index()

        # Check for existing version
        for existing in index.versions:
            if existing.version == version_num:
                if not allow_overwrite:
                    raise VersionExistsError(existing)
                # Remove old entry for update
                index.versions = [v for v in index.versions if v.version != version_num]
                break

        # Ensure scores directory exists
        self.ensure_scores_dir()

        # Save config file
        filename = self._config_filename(version_num)
        filepath = os.path.join(self.scores_dir, filename)

        # Convert to dict for YAML serialization
        config_dict = config.model_dump(mode="json", by_alias=True, exclude_none=True)
        with open(filepath, "w") as f:
            yaml.safe_dump(config_dict, f, default_flow_style=False, sort_keys=False)

        # Create version metadata
        now = datetime.now(tz=UTC)
        version_meta = ConfigVersion(
            version=version_num,
            timestamp=now,
            file=filename,
            description=config.version.description,
            notes=config.version.notes,
        )

        # Update index
        index.versions.append(version_meta)
        index.versions.sort(key=lambda v: v.timestamp, reverse=True)
        self._index = index
        self._save_index()

        # Update cache
        self._cache[version_num] = config

        log.info("Saved config version %s to %s", version_num, filepath)
        return version_meta

    def backup_active(self) -> ConfigVersion | None:
        """
        Backup the current active configuration to .scores.

        Returns the version metadata if successful, None if no active config.
        """
        if not os.path.exists(self._paths.config_path):
            return None

        # Load current config
        with open(self._paths.config_path) as f:
            if self._paths.config_path.endswith((".yaml", ".yml")):
                data = yaml.safe_load(f)
            else:
                data = json.load(f)

        config = ConductorConfig(**data)

        # If no version, we can't back it up properly
        if not config.version:
            log.warning("Active config has no version info, cannot backup")
            return None

        # Save to scores - allow overwrite for backups
        return self.save(config, allow_overwrite=True)

    def activate(self, version: str) -> ConductorConfig:
        """
        Make a stored version the active configuration.

        Copies the versioned config to CONDUCTOR_HOME/conductor.yaml.

        Args:
            version: Version number to activate.

        Returns:
            The activated ConductorConfig.

        Raises:
            VersionNotFoundError: If version doesn't exist.
        """
        config = self.get_or_raise(version)

        # Backup current active config first
        self.backup_active()

        # Write to active config path
        config_dict = config.model_dump(mode="json", by_alias=True, exclude_none=True)
        with open(self._paths.config_yaml_path, "w") as f:
            yaml.safe_dump(config_dict, f, default_flow_style=False, sort_keys=False)

        log.info("Activated config version %s", version)
        return config

    # ── Diff Operations ────────────────────────────────────────────────────────

    def diff(
        self,
        version_a: str,
        version_b: str,
        *,
        context_lines: int = 3,
    ) -> str:
        """
        Generate a unified diff between two config versions.

        Args:
            version_a: First version number (older).
            version_b: Second version number (newer).
            context_lines: Number of context lines in diff.

        Returns:
            Unified diff string.

        Raises:
            VersionNotFoundError: If either version doesn't exist.
        """
        config_a = self.get_or_raise(version_a)
        config_b = self.get_or_raise(version_b)

        # Convert to YAML for human-readable diff
        yaml_a = yaml.safe_dump(
            config_a.model_dump(mode="json", by_alias=True, exclude_none=True),
            default_flow_style=False,
            sort_keys=True,
        )
        yaml_b = yaml.safe_dump(
            config_b.model_dump(mode="json", by_alias=True, exclude_none=True),
            default_flow_style=False,
            sort_keys=True,
        )

        diff_lines = difflib.unified_diff(
            yaml_a.splitlines(keepends=True),
            yaml_b.splitlines(keepends=True),
            fromfile=f"conductor.{version_a}.yaml",
            tofile=f"conductor.{version_b}.yaml",
            n=context_lines,
        )

        return "".join(diff_lines)

    def diff_with_active(self, version: str, *, context_lines: int = 3) -> str:
        """
        Generate a diff between a stored version and the active config.

        Args:
            version: Version to compare against active config.
            context_lines: Number of context lines in diff.

        Returns:
            Unified diff string.
        """
        # Load active config
        if not os.path.exists(self._paths.config_path):
            raise FileNotFoundError("No active configuration found")

        with open(self._paths.config_path) as f:
            if self._paths.config_path.endswith((".yaml", ".yml")):
                data = yaml.safe_load(f)
            else:
                data = json.load(f)

        active_config = ConductorConfig(**data)
        stored_config = self.get_or_raise(version)

        yaml_active = yaml.safe_dump(
            active_config.model_dump(mode="json", by_alias=True, exclude_none=True),
            default_flow_style=False,
            sort_keys=True,
        )
        yaml_stored = yaml.safe_dump(
            stored_config.model_dump(mode="json", by_alias=True, exclude_none=True),
            default_flow_style=False,
            sort_keys=True,
        )

        diff_lines = difflib.unified_diff(
            yaml_stored.splitlines(keepends=True),
            yaml_active.splitlines(keepends=True),
            fromfile=f"conductor.{version}.yaml",
            tofile="conductor.yaml (active)",
            n=context_lines,
        )

        return "".join(diff_lines)

    # ── Cleanup ────────────────────────────────────────────────────────────────

    def delete(self, version: str) -> None:
        """
        Delete a stored configuration version.

        Args:
            version: Version number to delete.

        Raises:
            VersionNotFoundError: If version doesn't exist.
        """
        index = self.load_index()

        # Find and remove from index
        found = False
        for v in index.versions:
            if v.version == version:
                found = True
                index.versions.remove(v)
                break

        if not found:
            raise VersionNotFoundError(version)

        # Remove file
        filepath = self._config_path(version)
        if os.path.exists(filepath):
            os.remove(filepath)

        # Update index
        self._index = index
        self._save_index()

        # Remove from cache
        self._cache.pop(version, None)

        log.info("Deleted config version %s", version)
