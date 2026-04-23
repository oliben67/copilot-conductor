"""Tests for GET /agents/{name} endpoint and ConPilot.get_agent()."""

import json
from pathlib import Path

import pytest

from con_pilot.conductor import ConPilot
from con_pilot.core.models import AgentDetailResponse
from con_pilot.v1.endpoints.agents import (
    AgentConfigModifyRequest,
    get_agent,
    get_agent_config,
    list_agent_configs,
    modify_agent_config,
)

# ── Shared config ─────────────────────────────────────────────────────────────

_CONFIG = {
    "models": {"authorized_models": ["test-model"], "default_model": "test-model"},
    "agent": {
        "conductor": {"name": "uppity", "active": True, "scope": "system"},
        "developer": {
            "name": "code-monkey",
            "description": "Writes code.",
            "active": True,
            "scope": "system",
            "sidekick": True,
            "instructions": "You write clean Python.",
            "permissions": ["file_create", "file_modify", "git_commit"],
        },
        "reviewer": {
            "name": "code-reviewer",
            "description": "Reviews pull requests.",
            "active": False,
            "scope": "project",
        },
    },
    "tasks": [
        {
            "name": "daily-review",
            "agent": "reviewer",
            "description": "Daily PR review.",
            "instructions": "Review all open PRs.",
            "cron": "0 9 * * *",
        },
        {
            "name": "nightly-cleanup",
            "agent": "developer",
            "description": "Clean up stale branches.",
            "instructions": "Delete merged branches older than 7 days.",
        },
        {
            "name": "another-dev-task",
            "agent": "developer",
            "description": "Run linting.",
            "instructions": "Run ruff and fix issues.",
        },
    ],
}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated CONDUCTOR_HOME with system agents directory."""
    (tmp_path / "conductor.json").write_text(json.dumps(_CONFIG, indent=2))
    system_agents = tmp_path / ".github" / "system" / "agents"
    system_agents.mkdir(parents=True)
    (system_agents / "conductor.agent.md").write_text("# conductor\n")
    (system_agents / "developer.agent.md").write_text("# developer\n")
    (tmp_path / ".github" / "trust.json").write_text(
        json.dumps({"conductor": str(tmp_path)}, indent=2)
    )
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    monkeypatch.delenv("PROJECT_NAME", raising=False)
    return tmp_path


@pytest.fixture()
def pilot(home: Path) -> ConPilot:
    return ConPilot(str(home))


# ── ConPilot.get_agent ────────────────────────────────────────────────────────


class TestGetAgent:
    def test_returns_agent_by_role_key(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("developer")
        assert isinstance(result, AgentDetailResponse)
        assert result.role == "developer"

    def test_returns_agent_by_display_name(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("code-monkey")
        assert result is not None
        assert result.role == "developer"

    def test_display_name_lookup_is_case_insensitive(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("CODE-MONKEY")
        assert result is not None
        assert result.role == "developer"

    def test_returns_none_for_unknown_name(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("nonexistent-agent")
        assert result is None

    def test_includes_description(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("developer")
        assert result is not None
        assert result.description == "Writes code."

    def test_includes_instructions(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("developer")
        assert result is not None
        assert result.instructions == "You write clean Python."

    def test_includes_permissions(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("developer")
        assert result is not None
        assert "file_create" in result.permissions
        assert "file_modify" in result.permissions
        assert "git_commit" in result.permissions

    def test_active_flag(self, pilot: ConPilot) -> None:
        active = pilot.get_agent("developer")
        assert active is not None
        assert active.active is True

        inactive = pilot.get_agent("reviewer")
        assert inactive is not None
        assert inactive.active is False

    def test_tasks_assigned_to_agent(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("developer")
        assert result is not None
        task_names = {t.name for t in result.tasks}
        assert task_names == {"nightly-cleanup", "another-dev-task"}

    def test_tasks_for_reviewer_agent(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("reviewer")
        assert result is not None
        assert len(result.tasks) == 1
        assert result.tasks[0].name == "daily-review"
        assert result.tasks[0].cron == "0 9 * * *"

    def test_no_tasks_for_conductor(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("conductor")
        assert result is not None
        assert result.tasks == []

    def test_file_exists_for_system_agent_with_file(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("developer")
        assert result is not None
        assert result.file_exists is True
        assert result.file_path is not None

    def test_file_exists_false_for_missing_file(self, pilot: ConPilot) -> None:
        # reviewer has no .agent.md file created
        result = pilot.get_agent("reviewer")
        assert result is not None
        # reviewer is project-scoped — file_exists is False without project context
        assert result.file_exists is False

    def test_scope_returned(self, pilot: ConPilot) -> None:
        sys_agent = pilot.get_agent("developer")
        assert sys_agent is not None
        assert sys_agent.scope == "system"

        proj_agent = pilot.get_agent("reviewer")
        assert proj_agent is not None
        assert proj_agent.scope == "project"

    def test_sidekick_flag(self, pilot: ConPilot) -> None:
        result = pilot.get_agent("developer")
        assert result is not None
        assert result.sidekick is True

    def test_list_agent_configs_returns_runtime_agents(self, pilot: ConPilot) -> None:
        configs = pilot.list_agent_configs()
        assert "developer" in configs
        assert isinstance(configs["developer"], AgentDetailResponse)

    def test_get_agent_config_by_display_name(self, pilot: ConPilot) -> None:
        result = pilot.get_agent_config("code-monkey")
        assert result is not None
        assert result.role == "developer"
        assert result.running is False

    def test_update_agent_config_persists_to_file(self, pilot: ConPilot, home: Path) -> None:
        updated = pilot.update_agent_config(
            "developer",
            {"active": False, "model": "gpt-test", "description": "Updated desc"},
        )
        assert updated is not None
        assert updated.active is False
        assert updated.model == "gpt-test"
        assert updated.description == "Updated desc"

        config_data = json.loads((home / "conductor.json").read_text())
        assert config_data["agent"]["developer"]["active"] is False
        assert config_data["agent"]["developer"]["model"] == "gpt-test"
        assert config_data["agent"]["developer"]["description"] == "Updated desc"

    def test_update_agent_config_returns_none_for_unknown(self, pilot: ConPilot) -> None:
        updated = pilot.update_agent_config("ghost", {"active": True})
        assert updated is None


# ── GET /agents/{name} endpoint ───────────────────────────────────────────────


class TestGetAgentEndpoint:
    def test_returns_200_for_known_agent(self, pilot: ConPilot) -> None:
        result = get_agent("developer", pilot=pilot)
        assert isinstance(result, AgentDetailResponse)
        assert result.role == "developer"

    def test_returns_200_for_display_name(self, pilot: ConPilot) -> None:
        result = get_agent("code-monkey", pilot=pilot)
        assert result.role == "developer"

    def test_raises_404_for_unknown_agent(self, pilot: ConPilot) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            get_agent("ghost-agent", pilot=pilot)

        assert exc_info.value.status_code == 404
        assert "ghost-agent" in exc_info.value.detail

    def test_task_list_in_response(self, pilot: ConPilot) -> None:
        result = get_agent("developer", pilot=pilot)
        assert len(result.tasks) == 2

    def test_permissions_in_response(self, pilot: ConPilot) -> None:
        result = get_agent("developer", pilot=pilot)
        assert "file_create" in result.permissions


class TestAgentConfigEndpoints:
    def test_list_agent_configs_returns_all(self, pilot: ConPilot) -> None:
        result = list_agent_configs(pilot=pilot)
        assert "conductor" in result
        assert "developer" in result

    def test_get_agent_config_returns_agent(self, pilot: ConPilot) -> None:
        result = get_agent_config("developer", pilot=pilot)
        assert result.role == "developer"
        assert isinstance(result, AgentDetailResponse)

    def test_get_agent_config_raises_404_for_unknown(self, pilot: ConPilot) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            get_agent_config("ghost-agent", pilot=pilot)

        assert exc_info.value.status_code == 404

    def test_modify_agent_config_updates_values(self, pilot: ConPilot) -> None:
        body = AgentConfigModifyRequest(active=False, instructions="Do less, better.")
        result = modify_agent_config("developer", body=body, pilot=pilot)
        assert result.active is False
        assert result.instructions == "Do less, better."

    def test_modify_agent_config_rejects_empty_patch(self, pilot: ConPilot) -> None:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            modify_agent_config("developer", body=AgentConfigModifyRequest(), pilot=pilot)

        assert exc_info.value.status_code == 400
