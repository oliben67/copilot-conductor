"""Tests for the dev-build null-GUID admin key behaviour.

Dev builds (identified by ``CONDUCTOR_ENV=DEV``) must:
  * have ``ConPilot._load_or_generate_key()`` return the null-GUID string;
  * never persist a key file on disk;
  * accept the null-GUID via the ``X-Admin-Key`` header on protected endpoints.

Any other value of ``CONDUCTOR_ENV`` (or no value at all) is treated as a
normal/production build.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from con_pilot.conductor import ConPilot
from con_pilot.v1.endpoints.agents import verify_admin_key

NULL_GUID = "00000000-0000-0000-0000-000000000000"

_CONFIG = {
    "models": {"authorized_models": ["test-model"], "default_model": "test-model"},
    "agent": {
        "conductor": {"name": "uppity", "active": True, "scope": "system"},
    },
}


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "conductor.json").write_text(json.dumps(_CONFIG, indent=2))
    system_agents = tmp_path / ".github" / "system" / "agents"
    system_agents.mkdir(parents=True)
    (system_agents / "conductor.agent.md").write_text("# conductor\n")
    (tmp_path / ".github" / "trust.json").write_text(
        json.dumps({"conductor": str(tmp_path)}, indent=2)
    )
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("CONDUCTOR_ENV", raising=False)
    return tmp_path


@pytest.fixture()
def pilot(home: Path) -> ConPilot:
    return ConPilot(str(home))


class TestDevAdminKeyDetection:
    def test_dev_when_conductor_env_is_dev(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_ENV", "DEV")
        assert pilot._is_dev_build() is True

    def test_not_dev_when_conductor_env_unset(self, pilot: ConPilot) -> None:
        assert pilot._is_dev_build() is False

    def test_not_dev_when_conductor_env_other_values(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for value in ("", "dev", "Dev", "PROD", "prod", "1", "true", "DEV "):
            monkeypatch.setenv("CONDUCTOR_ENV", value)
            assert pilot._is_dev_build() is False, f"value={value!r}"


class TestDevLoadOrGenerateKey:
    def test_returns_null_guid_when_dev(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_ENV", "DEV")
        assert pilot._load_or_generate_key() == NULL_GUID

    def test_does_not_persist_key_file_when_dev(
        self, pilot: ConPilot, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_ENV", "DEV")
        pilot._load_or_generate_key()
        assert not os.path.exists(pilot.key_file)

    def test_ignores_existing_key_file_when_dev(
        self, pilot: ConPilot, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Even if a stale prod key happens to be on disk, dev mode wins.
        (home / "key").write_text("not-the-null-guid")
        monkeypatch.setenv("CONDUCTOR_ENV", "DEV")
        assert pilot._load_or_generate_key() == NULL_GUID

    def test_non_dev_generates_uuid_and_persists(self, pilot: ConPilot) -> None:
        key = pilot._load_or_generate_key()
        assert key != NULL_GUID
        assert len(key) == 36
        assert os.path.exists(pilot.key_file)
        # Stable across calls.
        assert pilot._load_or_generate_key() == key

    def test_non_dev_reads_existing_key_file(
        self, pilot: ConPilot, home: Path
    ) -> None:
        (home / "key").write_text("preset-admin-key\n")
        assert pilot._load_or_generate_key() == "preset-admin-key"

    def test_other_conductor_env_values_act_normally(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_ENV", "PROD")
        key = pilot._load_or_generate_key()
        assert key != NULL_GUID
        assert os.path.exists(pilot.key_file)


class TestDevAdminKeyEndpointAuth:
    def test_null_guid_accepted_in_dev(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_ENV", "DEV")
        result = verify_admin_key(x_admin_key=NULL_GUID, pilot=pilot)
        assert result is pilot

    def test_wrong_key_rejected_in_dev(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_ENV", "DEV")
        with pytest.raises(HTTPException) as exc:
            verify_admin_key(x_admin_key="wrong-key", pilot=pilot)
        assert exc.value.status_code == 403

    def test_missing_key_rejected_in_dev(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CONDUCTOR_ENV", "DEV")
        with pytest.raises(HTTPException) as exc:
            verify_admin_key(x_admin_key=None, pilot=pilot)
        assert exc.value.status_code == 401

    def test_null_guid_rejected_in_non_dev(self, pilot: ConPilot) -> None:
        with pytest.raises(HTTPException) as exc:
            verify_admin_key(x_admin_key=NULL_GUID, pilot=pilot)
        assert exc.value.status_code == 403
