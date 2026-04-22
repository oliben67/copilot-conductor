"""Tests covering every con-pilot command and key ConPilot behaviours.

Each test runs against an isolated CONDUCTOR_HOME under pytest's tmp_path;
the real ~/.conductor directory is never touched.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from con_pilot.conductor import ConPilot
from con_pilot.main import main
from con_pilot.models import ConductorConfig, CronConfig

# ── Minimal conductor.json shared across all tests ───────────────────────────

_CONFIG = {
    "models": {"authorized_models": ["test-model"], "default_model": "test-model"},
    "agent": {
        "conductor": {"name": "uppity", "active": True, "scope": "system"},
        "support": {
            "name": "dogsbody",
            "description": "Support agent.",
            "active": True,
            "scope": "system",
            "augmenting": True,
        },
        "developer": {
            "name": "code-monkey-[scope:project]-agent-[rank]",
            "description": "Developer agent.",
            "active": True,
            "scope": "project",
            "sidekick": True,
            "instances": {"max": 2},
        },
        "reviewer": {
            "name": "reviewer-[scope:project]",
            "description": "Reviewer agent.",
            "active": True,
            "scope": "project",
        },
    },
}

# Pre-built ConductorConfig model for tests that need to set _cfg directly
_CONFIG_MODEL = ConductorConfig(**_CONFIG)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated CONDUCTOR_HOME with a minimal directory layout."""
    (tmp_path / "conductor.json").write_text(json.dumps(_CONFIG, indent=2))
    (tmp_path / ".github" / "agents").mkdir(parents=True)
    system_agents = tmp_path / ".github" / "system" / "agents"
    system_agents.mkdir(parents=True)
    (system_agents / "conductor.agent.md").write_text(
        '---\nname: "uppity"\nmodel: "test-model"\n---\n'
    )
    (system_agents / "retired").mkdir()
    (system_agents / "logs").mkdir()
    (tmp_path / ".github" / "trust.json").write_text(
        json.dumps({"conductor": str(tmp_path)}, indent=2)
    )
    (tmp_path / "python" / "con-pilot").mkdir(parents=True)
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))
    monkeypatch.delenv("PROJECT_NAME", raising=False)
    return tmp_path


@pytest.fixture()
def pilot(home: Path) -> ConPilot:
    return ConPilot(str(home))


# ── CronConfig ───────────────────────────────────────────────────────────────


class TestCronConfig:
    def test_valid_daily_expression(self) -> None:
        cron = CronConfig(expression="0 9 * * *")
        assert cron.expression == "0 9 * * *"
        assert cron.file is None

    def test_valid_every_15_minutes(self) -> None:
        cron = CronConfig(expression="*/15 * * * *")
        assert cron.expression == "*/15 * * * *"

    def test_valid_weekly_expression(self) -> None:
        cron = CronConfig(expression="0 0 * * 0")
        assert cron.expression == "0 0 * * 0"

    def test_with_custom_file(self) -> None:
        cron = CronConfig(expression="0 9 * * *", file="custom.cron")
        assert cron.file == "custom.cron"

    def test_invalid_expression_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron expression"):
            CronConfig(expression="invalid")

    def test_invalid_expression_too_few_fields(self) -> None:
        with pytest.raises(ValueError, match="Invalid cron expression"):
            CronConfig(expression="0 9 *")


# ── _expand_name ─────────────────────────────────────────────────────────────


class TestExpandName:
    def test_both_placeholders(self, pilot: ConPilot) -> None:
        assert (
            pilot._expand_name("x-[scope:project]-[rank]", project="app", rank=2)
            == "x-app-2"
        )

    def test_missing_project_stripped(self, pilot: ConPilot) -> None:
        assert pilot._expand_name("reviewer-[scope:project]") == "reviewer"

    def test_missing_rank_stripped(self, pilot: ConPilot) -> None:
        assert pilot._expand_name("x-[rank]") == "x"

    def test_unknown_placeholder_removed(self, pilot: ConPilot) -> None:
        assert pilot._expand_name("foo-[unknown]-bar") == "foo-bar"

    def test_only_placeholders_yields_empty(self, pilot: ConPilot) -> None:
        assert pilot._expand_name("[scope:project]-[rank]") == ""


# ── _split_frontmatter ───────────────────────────────────────────────────────


class TestSplitFrontmatter:
    def test_splits_correctly(self, pilot: ConPilot) -> None:
        content = '---\nname: "x"\n---\n\n## Role\nBody.'
        fm, body = pilot._split_frontmatter(content)
        assert fm.endswith("---")
        assert "## Role" in body

    def test_no_frontmatter(self, pilot: ConPilot) -> None:
        fm, body = pilot._split_frontmatter("plain text")
        assert fm == ""
        assert body == "plain text"


# ── env property ─────────────────────────────────────────────────────────────


class TestEnv:
    def test_required_keys_present(self, pilot: ConPilot) -> None:
        for key in (
            "CONDUCTOR_HOME",
            "CONDUCTOR_AGENT_NAME",
            "SIDEKICK_AGENT_NAME",
            "COPILOT_DEFAULT_MODEL",
            "TRUSTED_DIRECTORIES",
        ):
            assert key in pilot.env, f"Missing key: {key}"

    def test_conductor_agent_name(self, pilot: ConPilot) -> None:
        assert pilot.env["CONDUCTOR_AGENT_NAME"] == "uppity"

    def test_sidekick_expanded_with_rank_1(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "myproj")
        assert pilot.env["SIDEKICK_AGENT_NAME"] == "code-monkey-myproj-agent-1"

    def test_trusted_dirs_contains_home(self, pilot: ConPilot, home: Path) -> None:
        assert str(home) in pilot.env["TRUSTED_DIRECTORIES"]

    def test_default_model(self, pilot: ConPilot) -> None:
        assert pilot.env["COPILOT_DEFAULT_MODEL"] == "test-model"


# ── list_agents ──────────────────────────────────────────────────────────────


class TestListAgents:
    def test_lists_system_agents(self, pilot: ConPilot, home: Path) -> None:
        result = pilot.list_agents()
        roles = {a.role for a in result.system_agents}
        assert "conductor" in roles
        assert "support" in roles

    def test_system_agent_file_exists(self, pilot: ConPilot, home: Path) -> None:
        result = pilot.list_agents()
        conductor = next(a for a in result.system_agents if a.role == "conductor")
        assert conductor.file_exists is True
        assert conductor.file_path is not None

    def test_system_agent_file_missing(self, pilot: ConPilot, home: Path) -> None:
        result = pilot.list_agents()
        support = next(a for a in result.system_agents if a.role == "support")
        # support.agent.md not created yet (no sync run)
        assert support.file_exists is False

    def test_lists_project_agents_with_project(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Register a project so trust.json has an entry
        proj = tmp_path / "myproj"
        proj.mkdir()
        pilot.register("myproj", str(proj))
        result = pilot.list_agents(project="myproj")
        roles = {a.role for a in result.project_agents}
        assert "developer" in roles
        assert "reviewer" in roles

    def test_multi_instance_agents(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        proj = tmp_path / "myproj"
        proj.mkdir()
        pilot.register("myproj", str(proj))
        result = pilot.list_agents(project="myproj")
        # developer has max=2, so should have 2 entries
        dev_agents = [a for a in result.project_agents if a.role == "developer"]
        assert len(dev_agents) == 2
        assert {a.instance for a in dev_agents} == {1, 2}

    def test_returns_agent_info_fields(self, pilot: ConPilot, home: Path) -> None:
        result = pilot.list_agents()
        conductor = next(a for a in result.system_agents if a.role == "conductor")
        assert conductor.name == "uppity"
        assert conductor.scope == "system"
        assert conductor.active is True

    def test_augmenting_flag_returned(self, pilot: ConPilot, home: Path) -> None:
        result = pilot.list_agents()
        support = next(a for a in result.system_agents if a.role == "support")
        assert support.augmenting is True
        conductor = next(a for a in result.system_agents if a.role == "conductor")
        assert conductor.augmenting is False


# ── sync ─────────────────────────────────────────────────────────────────────


class TestSync:
    def test_creates_system_agent(self, pilot: ConPilot, home: Path) -> None:
        with patch.object(pilot, "resolve_project", return_value=None):
            pilot.sync()
        assert (home / ".github" / "system" / "agents" / "support.agent.md").exists()

    def test_does_not_overwrite_conductor(self, pilot: ConPilot, home: Path) -> None:
        conductor_file = home / ".github" / "system" / "agents" / "conductor.agent.md"
        original = conductor_file.read_text()
        with patch.object(pilot, "resolve_project", return_value=None):
            pilot.sync()
        assert conductor_file.read_text() == original

    def test_retires_unknown_system_agent(self, pilot: ConPilot, home: Path) -> None:
        ghost = home / ".github" / "system" / "agents" / "ghost.agent.md"
        ghost.write_text("---\nname: ghost\n---\n")
        with patch.object(pilot, "resolve_project", return_value=None):
            pilot.sync()
        assert not ghost.exists()
        assert (home / ".github" / "system" / "agents" / "retired" / "ghost.agent.md").exists()

    def test_restores_retired_system_agent(self, pilot: ConPilot, home: Path) -> None:
        (home / ".github" / "system" / "agents" / "retired" / "support.agent.md").write_text(
            '---\nname: "dogsbody"\nmodel: "test-model"\n---\n'
        )
        with patch.object(pilot, "resolve_project", return_value=None):
            pilot.sync()
        assert (home / ".github" / "system" / "agents" / "support.agent.md").exists()

    def test_creates_project_agents(
        self, pilot: ConPilot, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        pilot.sync()
        p = home / ".github" / "projects" / "testproj" / "agents"
        assert (p / "developer.testproj.1.agent.md").exists()
        assert (p / "developer.testproj.2.agent.md").exists()
        assert (p / "reviewer.testproj.agent.md").exists()

    def test_creates_named_instances(
        self, pilot: ConPilot, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        pilot.sync()
        p = home / ".github" / "projects" / "testproj" / "agents"
        content1 = (p / "developer.testproj.1.agent.md").read_text()
        content2 = (p / "developer.testproj.2.agent.md").read_text()
        assert 'name: "code-monkey-testproj-agent-1"' in content1
        assert 'name: "code-monkey-testproj-agent-2"' in content2

    def test_retires_unknown_project_agent(
        self, pilot: ConPilot, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        p = home / ".github" / "projects" / "testproj" / "agents"
        p.mkdir(parents=True)
        (p / "retired").mkdir()
        stale = p / "old.testproj.agent.md"
        stale.write_text("---\nname: old\n---\n")
        pilot.sync()
        assert not stale.exists()
        assert (p / "retired" / "old.testproj.agent.md").exists()

    def test_idempotent(
        self, pilot: ConPilot, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        pilot.sync()
        p = home / ".github" / "projects" / "testproj" / "agents"
        files_before = {f.name for f in p.glob("*.agent.md")}
        pilot.sync()
        assert {f.name for f in p.glob("*.agent.md")} == files_before


# ── cron ─────────────────────────────────────────────────────────────────────


class TestCron:
    def test_no_cron_agents_no_raise(self, pilot: ConPilot) -> None:
        pilot._cfg = _CONFIG_MODEL
        pilot.cron()

    def test_creates_placeholder_cron_file(self, pilot: ConPilot, home: Path) -> None:
        pilot._cfg = ConductorConfig(
            **{
                "models": {
                    "authorized_models": ["test-model"],
                    "default_model": "test-model",
                },
                "agent": {
                    "conductor": {"name": "uppity", "active": True},
                    "support": {
                        "name": "dogsbody",
                        "active": True,
                        "scope": "system",
                        "cron": {"expression": "0 9 * * *"},
                    },
                },
            }
        )
        pilot.cron()
        assert (home / ".github" / "agents" / "cron" / "support.cron").exists()

    def test_skips_inactive_agent(self, pilot: ConPilot) -> None:
        pilot._cfg = ConductorConfig(
            **{
                "models": {
                    "authorized_models": ["test-model"],
                    "default_model": "test-model",
                },
                "agent": {
                    "conductor": {"name": "uppity", "active": True},
                    "support": {
                        "name": "dogsbody",
                        "active": False,
                        "scope": "system",
                        "cron": {"expression": "0 9 * * *"},
                    },
                },
            }
        )
        pilot.cron()  # should not raise or create any cron files


# ── register ─────────────────────────────────────────────────────────────────


class TestRegister:
    def test_creates_project_dirs(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        pilot.register("myapp", str(proj))
        assert (home / ".github" / "projects" / "myapp" / "agents").is_dir()
        assert (home / ".github" / "projects" / "myapp" / "cron").is_dir()

    def test_adds_to_trust_json(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        pilot.register("myapp", str(proj))
        trust = json.loads((home / ".github" / "trust.json").read_text())
        assert trust.get("myapp") == str(proj)

    def test_creates_agent_files(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        pilot.register("myapp", str(proj))
        p = home / ".github" / "projects" / "myapp" / "agents"
        assert (p / "developer.myapp.1.agent.md").exists()
        assert (p / "developer.myapp.2.agent.md").exists()
        assert (p / "reviewer.myapp.agent.md").exists()

    def test_idempotent(self, pilot: ConPilot, home: Path, tmp_path: Path) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        pilot.register("myapp", str(proj))
        pilot.register("myapp", str(proj))  # second call must not raise
        trust = json.loads((home / ".github" / "trust.json").read_text())
        assert list(trust.keys()).count("myapp") == 1


# ── retire_project ────────────────────────────────────────────────────────────


class TestRetireProject:
    def _register(self, pilot: ConPilot, tmp_path: Path, name: str = "myapp") -> Path:
        proj = tmp_path / name
        proj.mkdir(exist_ok=True)
        pilot.register(name, str(proj))
        return proj

    def test_moves_project_dir(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        self._register(pilot, tmp_path)
        pilot.retire_project("myapp")
        assert not (home / ".github" / "projects" / "myapp").exists()
        assert any((home / ".github" / "retired-projects").glob("myapp*"))

    def test_removes_from_trust(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        self._register(pilot, tmp_path)
        pilot.retire_project("myapp")
        trust = json.loads((home / ".github" / "trust.json").read_text())
        assert "myapp" not in trust

    def test_missing_project_dir_no_raise(self, pilot: ConPilot) -> None:
        pilot.retire_project("nonexistent")  # should not raise

    def test_collision_gets_timestamp_suffix(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        self._register(pilot, tmp_path, "myapp")
        retired_root = home / ".github" / "retired-projects"
        retired_root.mkdir(parents=True, exist_ok=True)
        # Pre-place a directory at the target destination to force collision
        (retired_root / "myapp").mkdir()
        pilot.retire_project("myapp")
        # Original collision dir plus the new timestamped one
        matches = list(retired_root.glob("myapp*"))
        assert len(matches) == 2


# ── amend_agent ───────────────────────────────────────────────────────────────


class TestAmendAgent:
    def _sync_proj(
        self, pilot: ConPilot, monkeypatch: pytest.MonkeyPatch, name: str = "testproj"
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", name)
        pilot.sync()

    def test_adds_instructions_section(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._sync_proj(pilot, monkeypatch)
        instr = tmp_path / "instr.md"
        instr.write_text("- Do A.\n- Do B.")
        pilot.amend_agent(str(instr), "developer", "testproj")
        content = (
            home
            / ".github"
            / "projects"
            / "testproj"
            / "agents"
            / "developer.testproj.1.agent.md"
        ).read_text()
        assert "## Instructions" in content
        assert "- Do A." in content

    def test_replaces_existing_instructions(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._sync_proj(pilot, monkeypatch)
        instr = tmp_path / "instr.md"
        instr.write_text("- Old.")
        pilot.amend_agent(str(instr), "developer", "testproj")
        instr.write_text("- New.")
        pilot.amend_agent(str(instr), "developer", "testproj")
        content = (
            home
            / ".github"
            / "projects"
            / "testproj"
            / "agents"
            / "developer.testproj.1.agent.md"
        ).read_text()
        assert "- New." in content
        assert "- Old." not in content

    def test_applies_to_all_instances(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        self._sync_proj(pilot, monkeypatch)
        instr = tmp_path / "instr.md"
        instr.write_text("- Write tests.")
        pilot.amend_agent(str(instr), "developer", "testproj")
        p = home / ".github" / "projects" / "testproj" / "agents"
        for i in (1, 2):
            assert (
                "## Instructions"
                in (p / f"developer.testproj.{i}.agent.md").read_text()
            )

    def test_conductor_always_blocked(self, pilot: ConPilot, tmp_path: Path) -> None:
        instr = tmp_path / "i.md"
        instr.write_text("override.")
        key = pilot._load_or_generate_key()
        with pytest.raises(ValueError, match="conductor"):
            pilot.amend_agent(str(instr), "conductor", key=key)

    def test_system_agent_requires_key(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        (home / ".github" / "system" / "agents" / "support.agent.md").write_text(
            '---\nname: "dogsbody"\nmodel: "test-model"\n---\n\n## Role\nSupport.'
        )
        instr = tmp_path / "i.md"
        instr.write_text("bad.")
        with pytest.raises(ValueError, match="system key"):
            pilot.amend_agent(str(instr), "support")

    def test_system_agent_with_correct_key(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        (home / ".github" / "system" / "agents" / "support.agent.md").write_text(
            '---\nname: "dogsbody"\nmodel: "test-model"\n---\n\n## Role\nSupport.'
        )
        instr = tmp_path / "i.md"
        instr.write_text("- Be helpful.")
        pilot.amend_agent(str(instr), "support", key=pilot._load_or_generate_key())
        content = (home / ".github" / "system" / "agents" / "support.agent.md").read_text()
        assert "## Instructions" in content
        assert "- Be helpful." in content


# ── replace_agent ─────────────────────────────────────────────────────────────


class TestReplaceAgent:
    def test_replaces_body_keeps_frontmatter(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        pilot.sync()
        instr = tmp_path / "i.md"
        instr.write_text("## New Body\nAll new.")
        pilot.replace_agent(str(instr), "reviewer", "testproj")
        content = (
            home
            / ".github"
            / "projects"
            / "testproj"
            / "agents"
            / "reviewer.testproj.agent.md"
        ).read_text()
        assert 'name: "reviewer-testproj"' in content
        assert "## New Body" in content
        assert "All new." in content

    def test_conductor_always_blocked(self, pilot: ConPilot, tmp_path: Path) -> None:
        instr = tmp_path / "i.md"
        instr.write_text("body.")
        with pytest.raises(ValueError, match="conductor"):
            pilot.replace_agent(
                str(instr), "conductor", key=pilot._load_or_generate_key()
            )

    def test_system_agent_requires_key(
        self, pilot: ConPilot, home: Path, tmp_path: Path
    ) -> None:
        (home / ".github" / "system" / "agents" / "support.agent.md").write_text(
            '---\nname: "dogsbody"\nmodel: "test-model"\n---\n\n## Role\nSupport.'
        )
        instr = tmp_path / "i.md"
        instr.write_text("body.")
        with pytest.raises(ValueError, match="system key"):
            pilot.replace_agent(str(instr), "support")


# ── reset_agent ───────────────────────────────────────────────────────────────


class TestResetAgent:
    def test_removes_instructions_section(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        pilot.sync()
        instr = tmp_path / "i.md"
        instr.write_text("- Do X.")
        pilot.amend_agent(str(instr), "developer", "testproj")
        pilot.reset_agent("developer", "testproj")
        content = (
            home
            / ".github"
            / "projects"
            / "testproj"
            / "agents"
            / "developer.testproj.1.agent.md"
        ).read_text()
        assert "## Instructions" not in content

    def test_resets_all_instances(
        self,
        pilot: ConPilot,
        home: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        pilot.sync()
        instr = tmp_path / "i.md"
        instr.write_text("- Custom.")
        pilot.amend_agent(str(instr), "developer", "testproj")
        pilot.reset_agent("developer", "testproj")
        p = home / ".github" / "projects" / "testproj" / "agents"
        for i in (1, 2):
            assert (
                "## Instructions"
                not in (p / f"developer.testproj.{i}.agent.md").read_text()
            )

    def test_uses_template_when_available(
        self, pilot: ConPilot, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PROJECT_NAME", "testproj")
        pilot.sync()
        templates = home / ".github" / "agents" / "templates"
        templates.mkdir(exist_ok=True)
        (templates / "developer.agent.md").write_text(
            '---\nname: "PLACEHOLDER"\nmodel: "PLACEHOLDER"\n---\n\n'
            "You are **PLACEHOLDER**, custom template body."
        )
        pilot.reset_agent("developer", "testproj")
        content = (
            home
            / ".github"
            / "projects"
            / "testproj"
            / "agents"
            / "developer.testproj.1.agent.md"
        ).read_text()
        assert "custom template body" in content

    def test_conductor_always_blocked(self, pilot: ConPilot) -> None:
        with pytest.raises(ValueError, match="conductor"):
            pilot.reset_agent("conductor")

    def test_system_agent_requires_key(self, pilot: ConPilot, home: Path) -> None:
        (home / ".github" / "system" / "agents" / "support.agent.md").write_text(
            '---\nname: "dogsbody"\nmodel: "test-model"\n---\n\n## Role\nSupport.'
        )
        with pytest.raises(ValueError, match="system key"):
            pilot.reset_agent("support")


# ── CLI dispatch ─────────────────────────────────────────────────────────────


class TestCli:
    def test_sync(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["con-pilot", "sync"])
        with patch("con_pilot.conductor.ConPilot.sync") as mock:
            main()
        mock.assert_called_once()

    def test_cron(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["con-pilot", "cron"])
        with patch("con_pilot.conductor.ConPilot.cron") as mock:
            main()
        mock.assert_called_once()

    def test_setup_env_default(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["con-pilot", "setup-env"])
        with patch("con_pilot.conductor.ConPilot.print_env") as mock:
            main()
        mock.assert_called_once_with(shell=False)

    def test_setup_env_shell_flag(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["con-pilot", "setup-env", "--shell"])
        with patch("con_pilot.conductor.ConPilot.print_env") as mock:
            main()
        mock.assert_called_once_with(shell=True)

    def test_register(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["con-pilot", "register", "foo", "/tmp/foo"])
        with patch("con_pilot.conductor.ConPilot.register") as mock:
            main()
        mock.assert_called_once_with("foo", "/tmp/foo")

    def test_retire_project(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["con-pilot", "retire-project", "foo"])
        with patch("con_pilot.conductor.ConPilot.retire_project") as mock:
            main()
        mock.assert_called_once_with("foo")

    @pytest.mark.skip(reason="amend command disabled")
    def test_amend(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sys, "argv", ["con-pilot", "amend", "/tmp/f.md", "developer", "myproj"]
        )
        with patch("con_pilot.conductor.ConPilot.amend_agent") as mock:
            main()
        mock.assert_called_once_with("/tmp/f.md", "developer", "myproj", None)

    @pytest.mark.skip(reason="amend command disabled")
    def test_amend_with_key(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sys,
            "argv",
            ["con-pilot", "amend", "/tmp/f.md", "support", "--key", "secret"],
        )
        with patch("con_pilot.conductor.ConPilot.amend_agent") as mock:
            main()
        mock.assert_called_once_with("/tmp/f.md", "support", None, "secret")

    def test_replace(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sys, "argv", ["con-pilot", "replace", "/tmp/f.md", "reviewer", "myproj"]
        )
        with patch("con_pilot.conductor.ConPilot.replace_agent") as mock:
            main()
        mock.assert_called_once_with("/tmp/f.md", "reviewer", "myproj", None)

    def test_reset(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["con-pilot", "reset", "developer", "myproj"])
        with patch("con_pilot.conductor.ConPilot.reset_agent") as mock:
            main()
        mock.assert_called_once_with("developer", "myproj", None)

    def test_reset_with_key(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sys, "argv", ["con-pilot", "reset", "support", "--key", "mykey"]
        )
        with patch("con_pilot.conductor.ConPilot.reset_agent") as mock:
            main()
        mock.assert_called_once_with("support", None, "mykey")


# ── YAML Config Support ──────────────────────────────────────────────────────


class TestYamlConfig:
    """Tests for YAML configuration file support."""

    def test_loads_yaml_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that conductor.yaml is loaded correctly."""
        (tmp_path / "conductor.yaml").write_text(yaml.dump(_CONFIG))
        agents = tmp_path / ".github" / "agents"
        agents.mkdir(parents=True)
        (agents / "conductor.agent.md").write_text('---\nname: "uppity"\n---\n')
        (agents / "retired").mkdir()
        (tmp_path / ".github" / "trust.json").write_text(
            json.dumps({"conductor": str(tmp_path)}, indent=2)
        )
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        assert pilot.config.models.default_model == "test-model"
        assert "conductor" in pilot.config.agents

    def test_prefers_yaml_over_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that conductor.yaml is preferred over conductor.json."""
        # Write JSON with one model
        json_config = {
            **_CONFIG,
            "models": {**_CONFIG["models"], "default_model": "json-model"},
        }
        (tmp_path / "conductor.json").write_text(json.dumps(json_config, indent=2))

        # Write YAML with different model
        yaml_config = {
            **_CONFIG,
            "models": {**_CONFIG["models"], "default_model": "yaml-model"},
        }
        (tmp_path / "conductor.yaml").write_text(yaml.dump(yaml_config))

        agents = tmp_path / ".github" / "agents"
        agents.mkdir(parents=True)
        (agents / "conductor.agent.md").write_text('---\nname: "uppity"\n---\n')
        (agents / "retired").mkdir()
        (tmp_path / ".github" / "trust.json").write_text(
            json.dumps({"conductor": str(tmp_path)}, indent=2)
        )
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        # Should use YAML config, not JSON
        assert pilot.config.models.default_model == "yaml-model"

    def test_falls_back_to_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that conductor.json is used when no YAML exists."""
        (tmp_path / "conductor.json").write_text(json.dumps(_CONFIG, indent=2))
        agents = tmp_path / ".github" / "agents"
        agents.mkdir(parents=True)
        (agents / "conductor.agent.md").write_text('---\nname: "uppity"\n---\n')
        (agents / "retired").mkdir()
        (tmp_path / ".github" / "trust.json").write_text(
            json.dumps({"conductor": str(tmp_path)}, indent=2)
        )
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        assert pilot.config.models.default_model == "test-model"

    def test_config_path_returns_yaml_when_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that config_path returns .yaml path when it exists."""
        (tmp_path / "conductor.yaml").write_text(yaml.dump(_CONFIG))
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        assert pilot.config_path.endswith("conductor.yaml")

    def test_config_path_returns_json_when_no_yaml(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that config_path returns .json path when no .yaml exists."""
        (tmp_path / "conductor.json").write_text(json.dumps(_CONFIG, indent=2))
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        assert pilot.config_path.endswith("conductor.json")

    def test_validate_yaml_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that validate() works with YAML config files."""
        (tmp_path / "conductor.yaml").write_text(yaml.dump(_CONFIG))
        # Create schema directory and file
        schema_dir = tmp_path / "src" / "schemas"
        schema_dir.mkdir(parents=True)
        # Copy minimal valid schema
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "models": {"type": "object"},
                "agent": {"type": "object"},
            },
        }
        (schema_dir / "conductor.schema.json").write_text(json.dumps(schema))
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        result = pilot.validate()
        assert result.valid is True
