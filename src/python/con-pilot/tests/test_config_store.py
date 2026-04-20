"""Tests for ConfigStore configuration version management."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from con_pilot.conductor import ConPilot
from con_pilot.core.services.config_store import (
    VersionExistsError,
    VersionNotFoundError,
)
from con_pilot.models import ConductorConfig

# ── Minimal config shared across tests ───────────────────────────────────────

_BASE_CONFIG = {
    "models": {"authorized_models": ["test-model"], "default_model": "test-model"},
    "agent": {
        "conductor": {"name": "uppity", "active": True, "scope": "system"},
    },
}


def _make_config(version: str, description: str | None = None) -> dict:
    """Create a config dict with version info."""
    return {
        **_BASE_CONFIG,
        "version": {
            "number": version,
            "description": description or f"Test version {version}",
            "date": datetime.now(tz=UTC).isoformat(),
        },
    }


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def pilot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConPilot:
    """Isolated ConPilot with minimal directory structure."""
    (tmp_path / "conductor.yaml").write_text(yaml.dump(_make_config("1.0.0")))
    agents = tmp_path / ".github" / "agents"
    agents.mkdir(parents=True)
    (agents / "conductor.agent.md").write_text('---\nname: "uppity"\n---\n')
    (agents / "retired").mkdir()
    (tmp_path / ".github" / "trust.json").write_text(
        json.dumps({"conductor": str(tmp_path)}, indent=2)
    )
    # Create schema directory and file
    schema_dir = tmp_path / "src" / "schemas"
    schema_dir.mkdir(parents=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "version": {"type": "object"},
            "models": {"type": "object"},
            "agent": {"type": "object"},
        },
    }
    (schema_dir / "conductor.schema.json").write_text(json.dumps(schema))
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    return ConPilot(str(tmp_path))


# ── ConfigStore Tests ────────────────────────────────────────────────────────


class TestConfigStoreInit:
    """Tests for ConfigStore initialization."""

    def test_creates_scores_dir(self, pilot: ConPilot, tmp_path: Path) -> None:
        """Test that ensure_scores_dir creates the directory."""
        store = pilot.config_store
        scores_path = Path(store.scores_dir)

        assert not scores_path.exists()
        store.ensure_scores_dir()
        assert scores_path.exists()

    def test_load_index_creates_empty(self, pilot: ConPilot) -> None:
        """Test that load_index creates empty index if none exists."""
        store = pilot.config_store
        index = store.load_index()
        assert index.versions == []

    def test_load_index_reads_existing(self, pilot: ConPilot, tmp_path: Path) -> None:
        """Test that load_index reads existing index file."""
        store = pilot.config_store
        store.ensure_scores_dir()

        # Manually create an index
        index_data = {
            "versions": [
                {
                    "version": "1.0.0",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "file": "conductor.1.0.0.yaml",
                    "description": "Initial",
                }
            ]
        }
        (tmp_path / ".scores" / "index.json").write_text(json.dumps(index_data))

        # Force reload
        store._index = None
        index = store.load_index()
        assert len(index.versions) == 1
        assert index.versions[0].version == "1.0.0"


class TestConfigStoreSave:
    """Tests for saving configurations."""

    def test_save_new_version(self, pilot: ConPilot) -> None:
        """Test saving a new configuration version."""
        store = pilot.config_store
        config = ConductorConfig(**_make_config("2.0.0", "New version"))

        version_meta = store.save(config)

        assert version_meta.version == "2.0.0"
        assert version_meta.description == "New version"
        assert Path(store.scores_dir, "conductor.2.0.0.yaml").exists()

    def test_save_duplicate_fails(self, pilot: ConPilot) -> None:
        """Test that saving duplicate version fails without overwrite."""
        store = pilot.config_store
        config = ConductorConfig(**_make_config("2.0.0"))

        store.save(config)

        with pytest.raises(VersionExistsError) as exc:
            store.save(config)

        assert "2.0.0" in str(exc.value)

    def test_save_with_overwrite(self, pilot: ConPilot) -> None:
        """Test that saving with overwrite updates existing version."""
        store = pilot.config_store
        config1 = ConductorConfig(**_make_config("2.0.0", "First"))
        config2 = ConductorConfig(**_make_config("2.0.0", "Updated"))

        store.save(config1)
        version_meta = store.save(config2, allow_overwrite=True)

        assert version_meta.description == "Updated"
        assert len([v for v in store.versions if v.version == "2.0.0"]) == 1

    def test_save_without_version_fails(self, pilot: ConPilot) -> None:
        """Test that saving config without version raises ValueError."""
        store = pilot.config_store
        config = ConductorConfig(**_BASE_CONFIG)  # No version

        with pytest.raises(ValueError, match="must have version"):
            store.save(config)


class TestConfigStoreGet:
    """Tests for retrieving configurations."""

    def test_get_existing_version(self, pilot: ConPilot) -> None:
        """Test getting an existing version."""
        store = pilot.config_store
        config = ConductorConfig(**_make_config("2.0.0"))
        store.save(config)

        retrieved = store.get("2.0.0")

        assert retrieved is not None
        assert retrieved.version.number == "2.0.0"

    def test_get_nonexistent_returns_none(self, pilot: ConPilot) -> None:
        """Test that getting nonexistent version returns None."""
        store = pilot.config_store
        assert store.get("99.0.0") is None

    def test_get_or_raise_existing(self, pilot: ConPilot) -> None:
        """Test get_or_raise with existing version."""
        store = pilot.config_store
        config = ConductorConfig(**_make_config("2.0.0"))
        store.save(config)

        retrieved = store.get_or_raise("2.0.0")
        assert retrieved.version.number == "2.0.0"

    def test_get_or_raise_missing(self, pilot: ConPilot) -> None:
        """Test get_or_raise with missing version raises."""
        store = pilot.config_store

        with pytest.raises(VersionNotFoundError):
            store.get_or_raise("99.0.0")


class TestConfigStoreBackup:
    """Tests for backing up active configuration."""

    def test_backup_active(self, pilot: ConPilot) -> None:
        """Test backing up the active configuration."""
        store = pilot.config_store

        version_meta = store.backup_active()

        assert version_meta is not None
        assert version_meta.version == "1.0.0"  # From fixture
        assert Path(store.scores_dir, "conductor.1.0.0.yaml").exists()

    def test_backup_active_no_version_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test backup returns None if active config has no version."""
        # Create config without version
        (tmp_path / "conductor.yaml").write_text(yaml.dump(_BASE_CONFIG))
        agents = tmp_path / ".github" / "agents"
        agents.mkdir(parents=True)
        (agents / "conductor.agent.md").write_text('---\nname: "uppity"\n---\n')
        (agents / "retired").mkdir()
        (tmp_path / ".github" / "trust.json").write_text(
            json.dumps({"conductor": str(tmp_path)}, indent=2)
        )
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        version_meta = pilot.config_store.backup_active()

        assert version_meta is None


class TestConfigStoreActivate:
    """Tests for activating stored configurations."""

    def test_activate_version(self, pilot: ConPilot, tmp_path: Path) -> None:
        """Test activating a stored version."""
        store = pilot.config_store

        # Save a new version
        config = ConductorConfig(**_make_config("2.0.0", "New active"))
        store.save(config)

        # Activate it
        activated = store.activate("2.0.0")

        assert activated.version.number == "2.0.0"

        # Check the active config was updated
        with open(tmp_path / "conductor.yaml") as f:
            active_data = yaml.safe_load(f)
        assert active_data["version"]["number"] == "2.0.0"

    def test_activate_nonexistent_fails(self, pilot: ConPilot) -> None:
        """Test activating nonexistent version raises."""
        store = pilot.config_store

        with pytest.raises(VersionNotFoundError):
            store.activate("99.0.0")


class TestConfigStoreDiff:
    """Tests for diffing configurations."""

    def test_diff_two_versions(self, pilot: ConPilot) -> None:
        """Test generating diff between two versions."""
        store = pilot.config_store

        # Save two versions
        config1 = ConductorConfig(**_make_config("1.0.0", "First"))
        config2 = ConductorConfig(**_make_config("2.0.0", "Second"))
        store.save(config1)
        store.save(config2)

        diff_text = store.diff("1.0.0", "2.0.0")

        assert "conductor.1.0.0.yaml" in diff_text
        assert "conductor.2.0.0.yaml" in diff_text
        # Should show version number change
        assert "-number: 1.0.0" in diff_text or "1.0.0" in diff_text
        assert "+number: 2.0.0" in diff_text or "2.0.0" in diff_text

    def test_diff_nonexistent_fails(self, pilot: ConPilot) -> None:
        """Test diff with nonexistent version raises."""
        store = pilot.config_store
        config = ConductorConfig(**_make_config("1.0.0"))
        store.save(config)

        with pytest.raises(VersionNotFoundError):
            store.diff("1.0.0", "99.0.0")


class TestConfigStoreDelete:
    """Tests for deleting configurations."""

    def test_delete_version(self, pilot: ConPilot) -> None:
        """Test deleting a stored version."""
        store = pilot.config_store
        config = ConductorConfig(**_make_config("2.0.0"))
        store.save(config)

        # Verify it exists
        assert store.get("2.0.0") is not None

        # Delete it
        store.delete("2.0.0")

        # Verify it's gone
        assert store.get("2.0.0") is None
        assert not Path(store.scores_dir, "conductor.2.0.0.yaml").exists()

    def test_delete_nonexistent_fails(self, pilot: ConPilot) -> None:
        """Test deleting nonexistent version raises."""
        store = pilot.config_store

        with pytest.raises(VersionNotFoundError):
            store.delete("99.0.0")


class TestConfigStoreLoadAll:
    """Tests for bulk loading configurations."""

    def test_load_all_populates_cache(self, pilot: ConPilot) -> None:
        """Test that load_all loads all versions into cache."""
        store = pilot.config_store

        # Save multiple versions
        for v in ["1.0.0", "2.0.0", "3.0.0"]:
            config = ConductorConfig(**_make_config(v))
            store.save(config)

        # Clear cache
        store._cache.clear()

        # Load all
        cache = store.load_all()

        assert len(cache) == 3
        assert "1.0.0" in cache
        assert "2.0.0" in cache
        assert "3.0.0" in cache
