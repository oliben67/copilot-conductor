"""Integration tests that invoke the con-pilot CLI as a subprocess.

Every test runs against an isolated CONDUCTOR_HOME under pytest's tmp_path.
The real ~/.conductor directory is never touched.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from con_pilot.conductor.paths import resolve_key_file

# Resolve the con-pilot entry-point script from the same venv as the test runner.
_BIN = Path(sys.executable).parent / "con-pilot"

_CONFIG = {
    "models": {"authorized_models": ["test-model"], "default_model": "test-model"},
    "agent": {
        "conductor": {
            "name": "uppity",
            "active": True,
            "scope": "system",
            "cron": {"expression": "*/15 * * * *"},
        },
        "support": {
            "name": "dogsbody",
            "description": "Support agent.",
            "active": True,
            "scope": "system",
            "cron": {"expression": "*/30 * * * *"},
            "augmenting": True,
        },
        "arbitrator": {
            "name": "sir",
            "description": "Arbitrator agent.",
            "active": True,
            "scope": "system",
        },
        "developer": {
            "name": "code-monkey-[scope]-agent-[rank]",
            "description": "Developer agent.",
            "active": True,
            "scope": "project",
            "sidekick": True,
            "instances": {"max": 2},
        },
        "reviewer": {
            "name": "reviewer-[scope]",
            "description": "Reviewer agent.",
            "active": True,
            "scope": "project",
        },
    },
}


def _run(
    *args: str,
    home: Path,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run con-pilot with the given subcommand args against an isolated home."""
    env = {**os.environ, "CONDUCTOR_HOME": str(home)}
    env.pop("PROJECT_NAME", None)
    return subprocess.run(
        [str(_BIN), *args],
        env=env,
        capture_output=True,
        text=True,
        check=check,
        input=input_text,
    )


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """Isolated CONDUCTOR_HOME with minimal layout."""
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
    return tmp_path


# ── help ─────────────────────────────────────────────────────────────────────


class TestHelp:
    def test_no_args_shows_help(self, home: Path) -> None:
        r = _run(home=home)
        assert r.returncode == 0
        assert "usage:" in r.stdout.lower() or "con-pilot" in r.stdout.lower()

    def test_help_subcommand(self, home: Path) -> None:
        r = _run("help", home=home)
        assert r.returncode == 0
        assert "sync" in r.stdout


# ── list-agents ──────────────────────────────────────────────────────────────


class TestListAgents:
    def test_lists_system_agents(self, home: Path) -> None:
        r = _run("list-agents", home=home)
        assert r.returncode == 0
        assert "conductor" in r.stdout
        assert "support" in r.stdout

    def test_json_output(self, home: Path) -> None:
        r = _run("list-agents", "--json", home=home)
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert "system_agents" in data
        assert "project_agents" in data

    def test_project_filter(self, home: Path, tmp_path: Path) -> None:
        proj = tmp_path / "testproj"
        proj.mkdir()
        _run("register", "testproj", str(proj), home=home)
        r = _run("list-agents", "--project", "testproj", home=home)
        assert r.returncode == 0
        assert "developer" in r.stdout


# ── sync ─────────────────────────────────────────────────────────────────────


class TestSync:
    def test_creates_system_agents(self, home: Path) -> None:
        _run("sync", home=home)
        assert (home / ".github" / "system" / "agents" / "support.agent.md").exists()
        assert (home / ".github" / "system" / "agents" / "arbitrator.agent.md").exists()

    def test_does_not_overwrite_conductor(self, home: Path) -> None:
        conductor = home / ".github" / "system" / "agents" / "conductor.agent.md"
        original = conductor.read_text()
        _run("sync", home=home)
        assert conductor.read_text() == original

    def test_retires_unknown_agent(self, home: Path) -> None:
        ghost = home / ".github" / "system" / "agents" / "ghost.agent.md"
        ghost.write_text("---\nname: ghost\n---\n")
        _run("sync", home=home)
        assert not ghost.exists()
        assert (
            home / ".github" / "system" / "agents" / "retired" / "ghost.agent.md"
        ).exists()

    def test_restores_retired_agent(self, home: Path) -> None:
        (
            home / ".github" / "system" / "agents" / "retired" / "support.agent.md"
        ).write_text('---\nname: "dogsbody"\nmodel: "test-model"\n---\n')
        _run("sync", home=home)
        assert (home / ".github" / "system" / "agents" / "support.agent.md").exists()
        assert not (
            home / ".github" / "system" / "agents" / "retired" / "support.agent.md"
        ).exists()

    def test_creates_project_agents(self, home: Path) -> None:
        env_extra = {"PROJECT_NAME": "myproj"}
        env = {**os.environ, "CONDUCTOR_HOME": str(home), **env_extra}
        subprocess.run(
            [str(_BIN), "sync"], env=env, capture_output=True, text=True, check=True
        )
        p = home / ".github" / "projects" / "myproj" / "agents"
        assert (p / "developer.myproj.1.agent.md").exists()
        assert (p / "developer.myproj.2.agent.md").exists()
        assert (p / "reviewer.myproj.agent.md").exists()

    def test_idempotent(self, home: Path) -> None:
        _run("sync", home=home)
        agents_before = set((home / ".github" / "system" / "agents").glob("*.agent.md"))
        _run("sync", home=home)
        agents_after = set((home / ".github" / "system" / "agents").glob("*.agent.md"))
        assert agents_before == agents_after


# ── cron ─────────────────────────────────────────────────────────────────────


class TestCron:
    def test_runs_without_error(self, home: Path) -> None:
        r = _run("cron", home=home)
        assert r.returncode == 0

    def test_creates_cron_files(self, home: Path) -> None:
        _run("cron", home=home)
        assert (home / ".github" / "agents" / "cron" / "conductor.cron").exists()
        assert (home / ".github" / "agents" / "cron" / "support.cron").exists()


# ── setup-env ────────────────────────────────────────────────────────────────


class TestSetupEnv:
    def test_outputs_key_value_pairs(self, home: Path) -> None:
        r = _run("setup-env", home=home)
        assert r.returncode == 0
        lines = [line for line in r.stdout.strip().splitlines() if "=" in line]
        env = dict(line.split("=", 1) for line in lines)
        assert env["CONDUCTOR_HOME"] == str(home)
        assert env["CONDUCTOR_AGENT_NAME"] == "uppity"
        assert env["COPILOT_DEFAULT_MODEL"] == "test-model"

    def test_shell_flag(self, home: Path) -> None:
        r = _run("setup-env", "--shell", home=home)
        assert r.returncode == 0
        assert 'export CONDUCTOR_HOME="' in r.stdout
        assert 'export CONDUCTOR_AGENT_NAME="uppity"' in r.stdout


# ── register ─────────────────────────────────────────────────────────────────


class TestRegister:
    def test_creates_project_structure(self, home: Path, tmp_path: Path) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        _run("register", "myapp", str(proj), home=home)
        assert (home / ".github" / "projects" / "myapp" / "agents").is_dir()
        assert (home / ".github" / "projects" / "myapp" / "cron").is_dir()

    def test_updates_trust_json(self, home: Path, tmp_path: Path) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        _run("register", "myapp", str(proj), home=home)
        trust = json.loads((home / ".github" / "trust.json").read_text())
        assert trust["myapp"] == str(proj)

    def test_creates_agent_files(self, home: Path, tmp_path: Path) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        _run("register", "myapp", str(proj), home=home)
        p = home / ".github" / "projects" / "myapp" / "agents"
        assert (p / "developer.myapp.1.agent.md").exists()
        assert (p / "developer.myapp.2.agent.md").exists()
        assert (p / "reviewer.myapp.agent.md").exists()

    def test_idempotent(self, home: Path, tmp_path: Path) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()
        _run("register", "myapp", str(proj), home=home)
        r = _run("register", "myapp", str(proj), home=home)
        assert r.returncode == 0


# ── retire-project ───────────────────────────────────────────────────────────


class TestRetireProject:
    def _register(self, home: Path, tmp_path: Path, name: str = "myapp") -> Path:
        proj = tmp_path / name
        proj.mkdir(exist_ok=True)
        _run("register", name, str(proj), home=home)
        return proj

    def test_moves_project_dir(self, home: Path, tmp_path: Path) -> None:
        self._register(home, tmp_path)
        _run("retire-project", "myapp", home=home)
        assert not (home / ".github" / "projects" / "myapp").exists()
        assert any((home / ".github" / "retired-projects").glob("myapp*"))

    def test_removes_from_trust(self, home: Path, tmp_path: Path) -> None:
        self._register(home, tmp_path)
        _run("retire-project", "myapp", home=home)
        trust = json.loads((home / ".github" / "trust.json").read_text())
        assert "myapp" not in trust

    def test_missing_project_no_error(self, home: Path) -> None:
        r = _run("retire-project", "nonexistent", home=home)
        assert r.returncode == 0


# ── amend ────────────────────────────────────────────────────────────────────


@pytest.mark.skip(reason="amend command disabled")
class TestAmend:
    def _sync_proj(self, home: Path, project: str = "testproj") -> None:
        env = {**os.environ, "CONDUCTOR_HOME": str(home), "PROJECT_NAME": project}
        subprocess.run(
            [str(_BIN), "sync"], env=env, capture_output=True, text=True, check=True
        )

    def test_appends_instructions(self, home: Path, tmp_path: Path) -> None:
        self._sync_proj(home)
        instr = tmp_path / "instr.md"
        instr.write_text("- Do A.\n- Do B.")
        _run("amend", str(instr), "developer", "testproj", home=home)
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

    def test_applies_to_all_instances(self, home: Path, tmp_path: Path) -> None:
        self._sync_proj(home)
        instr = tmp_path / "instr.md"
        instr.write_text("- Write tests.")
        _run("amend", str(instr), "developer", "testproj", home=home)
        p = home / ".github" / "projects" / "testproj" / "agents"
        for i in (1, 2):
            assert (
                "## Instructions"
                in (p / f"developer.testproj.{i}.agent.md").read_text()
            )

    def test_conductor_blocked(self, home: Path, tmp_path: Path) -> None:
        instr = tmp_path / "i.md"
        instr.write_text("override.")
        key_path = Path(resolve_key_file(str(home)))
        key = key_path.read_text().strip() if key_path.exists() else "anykey"
        r = _run("amend", str(instr), "conductor", "--key", key, home=home, check=False)
        assert r.returncode != 0

    def test_system_agent_requires_key(self, home: Path, tmp_path: Path) -> None:
        _run("sync", home=home)
        instr = tmp_path / "i.md"
        instr.write_text("bad.")
        r = _run("amend", str(instr), "support", home=home, check=False)
        assert r.returncode != 0

    def test_system_agent_with_correct_key(self, home: Path, tmp_path: Path) -> None:
        _run("sync", home=home)
        # Trigger key generation via a failing amend attempt
        instr = tmp_path / "i.md"
        instr.write_text("- Be helpful.")
        _run("amend", str(instr), "support", home=home, check=False)
        key_file = Path(resolve_key_file(str(home)))
        key = key_file.read_text().strip()
        _run("amend", str(instr), "support", "--key", key, home=home)
        content = (
            home / ".github" / "system" / "agents" / "support.agent.md"
        ).read_text()
        assert "## Instructions" in content
        assert "- Be helpful." in content

    def test_system_agent_with_key_in_appdir(self, home: Path, tmp_path: Path) -> None:
        _run("sync", home=home)
        appdir = home / "src" / "python" / "con-pilot" / "appimage" / "AppDir"
        appdir.mkdir(parents=True, exist_ok=True)
        (appdir / "key").write_text("appdir-test-key")

        instr = tmp_path / "i.md"
        instr.write_text("- From AppDir key.")
        _run("amend", str(instr), "support", "--key", "appdir-test-key", home=home)

        content = (
            home / ".github" / "system" / "agents" / "support.agent.md"
        ).read_text()
        assert "- From AppDir key." in content


# ── replace ──────────────────────────────────────────────────────────────────


class TestReplace:
    def _sync_proj(self, home: Path, project: str = "testproj") -> None:
        env = {**os.environ, "CONDUCTOR_HOME": str(home), "PROJECT_NAME": project}
        subprocess.run(
            [str(_BIN), "sync"], env=env, capture_output=True, text=True, check=True
        )

    def test_replaces_body_keeps_frontmatter(self, home: Path, tmp_path: Path) -> None:
        self._sync_proj(home)
        instr = tmp_path / "i.md"
        instr.write_text("## New Body\nAll new.")
        _run("replace", str(instr), "reviewer", "testproj", home=home)
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

    def test_conductor_blocked(self, home: Path, tmp_path: Path) -> None:
        instr = tmp_path / "i.md"
        instr.write_text("body.")
        r = _run("replace", str(instr), "conductor", home=home, check=False)
        assert r.returncode != 0

    def test_system_agent_requires_key(self, home: Path, tmp_path: Path) -> None:
        _run("sync", home=home)
        instr = tmp_path / "i.md"
        instr.write_text("body.")
        r = _run("replace", str(instr), "support", home=home, check=False)
        assert r.returncode != 0


# ── reset ────────────────────────────────────────────────────────────────────


class TestReset:
    def _sync_proj(self, home: Path, project: str = "testproj") -> None:
        env = {**os.environ, "CONDUCTOR_HOME": str(home), "PROJECT_NAME": project}
        subprocess.run(
            [str(_BIN), "sync"], env=env, capture_output=True, text=True, check=True
        )

    def test_removes_instructions(self, home: Path, tmp_path: Path) -> None:
        self._sync_proj(home)
        instr = tmp_path / "i.md"
        instr.write_text("- Custom.")
        _run("replace", str(instr), "developer", "testproj", home=home)
        _run("reset", "developer", "testproj", home=home)
        content = (
            home
            / ".github"
            / "projects"
            / "testproj"
            / "agents"
            / "developer.testproj.1.agent.md"
        ).read_text()
        assert "## Instructions" not in content

    def test_resets_all_instances(self, home: Path, tmp_path: Path) -> None:
        self._sync_proj(home)
        instr = tmp_path / "i.md"
        instr.write_text("- Custom.")
        _run("replace", str(instr), "developer", "testproj", home=home)
        _run("reset", "developer", "testproj", home=home)
        p = home / ".github" / "projects" / "testproj" / "agents"
        for i in (1, 2):
            assert (
                "## Instructions"
                not in (p / f"developer.testproj.{i}.agent.md").read_text()
            )

    def test_uses_template(self, home: Path) -> None:
        self._sync_proj(home)
        templates = home / ".github" / "agents" / "templates"
        templates.mkdir(exist_ok=True)
        (templates / "developer.agent.md").write_text(
            '---\nname: "PLACEHOLDER"\nmodel: "PLACEHOLDER"\n---\n\n'
            "You are **PLACEHOLDER**, custom template body."
        )
        _run("reset", "developer", "testproj", home=home)
        content = (
            home
            / ".github"
            / "projects"
            / "testproj"
            / "agents"
            / "developer.testproj.1.agent.md"
        ).read_text()
        assert "custom template body" in content

    def test_conductor_blocked(self, home: Path) -> None:
        r = _run("reset", "conductor", home=home, check=False)
        assert r.returncode != 0

    def test_system_agent_requires_key(self, home: Path) -> None:
        _run("sync", home=home)
        r = _run("reset", "support", home=home, check=False)
        assert r.returncode != 0


# ── Full workflow (register → replace → reset → retire) ─────────────────────


class TestFullWorkflow:
    def test_project_lifecycle(self, home: Path, tmp_path: Path) -> None:
        """End-to-end: register, sync, replace, reset, retire."""
        proj = tmp_path / "lifecycle-app"
        proj.mkdir()

        # Register
        _run("register", "lifecycle-app", str(proj), home=home)
        p = home / ".github" / "projects" / "lifecycle-app" / "agents"
        assert (p / "developer.lifecycle-app.1.agent.md").exists()
        assert (p / "reviewer.lifecycle-app.agent.md").exists()

        trust = json.loads((home / ".github" / "trust.json").read_text())
        assert "lifecycle-app" in trust

        # Replace developer
        instr = tmp_path / "instr.md"
        instr.write_text("## Custom\n- Follow TDD.\n- Write docs.")
        _run("replace", str(instr), "developer", "lifecycle-app", home=home)
        content = (p / "developer.lifecycle-app.1.agent.md").read_text()
        assert "- Follow TDD." in content

        # Reset
        _run("reset", "developer", "lifecycle-app", home=home)
        content = (p / "developer.lifecycle-app.1.agent.md").read_text()
        assert "## Instructions" not in content

        # Replace reviewer
        instr.write_text("## Custom\nFully replaced body.")
        _run("replace", str(instr), "reviewer", "lifecycle-app", home=home)
        content = (p / "reviewer.lifecycle-app.agent.md").read_text()
        assert "Fully replaced body." in content
        assert 'name: "reviewer-lifecycle-app"' in content

        # Retire
        _run("retire-project", "lifecycle-app", home=home)
        assert not (home / ".github" / "projects" / "lifecycle-app").exists()
        assert any((home / ".github" / "retired-projects").glob("lifecycle-app*"))
        trust = json.loads((home / ".github" / "trust.json").read_text())
        assert "lifecycle-app" not in trust
