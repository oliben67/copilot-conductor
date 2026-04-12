"""
conductor.py — ConPilot: the single facade for all con-pilot operations.

All functionality (bootstrap, config, agent sync, cron dispatch,
session-env resolution, watcher management, and HTTP service) lives here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

try:
    from croniter import croniter

    HAS_CRONITER = True
except ImportError:
    HAS_CRONITER = False

log = logging.getLogger(__name__)

_CRON_PLACEHOLDER = """\
# Cron jobs for the {role} agent.
# Format: TOML — add [[job]] blocks with name, schedule (cron expression), and task.
#
# Example:
# [[job]]
# name = "daily-check"
# schedule = "0 9 * * *"
# task = "Describe what the {role} agent should do at this time."
"""


class ConPilot:
    """
    Facade for all con-pilot operations.

    Instantiating ``ConPilot`` resolves ``CONDUCTOR_HOME`` and sets it in
    the process environment so all child processes inherit it.

    Parameters
    ----------
    conductor_home:
        Override the conductor home directory.  When omitted, resolved via
        (in order) the ``CONDUCTOR_HOME`` env var and self-location from
        the package path.
    """

    DEFAULT_INTERVAL: int = 15 * 60  # seconds (900)

    def __init__(self, conductor_home: str | None = None) -> None:
        self.home: str = self._resolve_home(conductor_home)
        self._cfg: dict | None = None

    # ── Bootstrap ──────────────────────────────────────────────────────────────

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
            if (candidate / "conductor.json").exists():
                home = str(candidate)
        if home:
            os.environ["CONDUCTOR_HOME"] = home
        return home

    # ── Paths (derived from self.home) ─────────────────────────────────────────

    @property
    def config_path(self) -> str:
        return os.path.join(self.home, "conductor.json")

    @property
    def agents_dir(self) -> str:
        return os.path.join(self.home, ".github", "agents")

    @property
    def retired_dir(self) -> str:
        return os.path.join(self.agents_dir, "retired")

    @property
    def cron_dir(self) -> str:
        return os.path.join(self.agents_dir, "cron")

    @property
    def cron_state_dir(self) -> str:
        return os.path.join(self.cron_dir, ".state")

    @property
    def pending_log(self) -> str:
        return os.path.join(self.cron_dir, "pending.log")

    @property
    def templates_dir(self) -> str:
        return os.path.join(self.agents_dir, "templates")

    @property
    def trust_file(self) -> str:
        return os.path.join(self.home, ".github", "trust.json")

    @property
    def key_file(self) -> str:
        return os.path.join(self.home, "python", "con-pilot", "key")

    def _load_or_generate_key(self) -> str:
        """Return the system key, generating and persisting a new GUID on first call."""
        import uuid  # noqa: PLC0415

        if os.path.exists(self.key_file):
            with open(self.key_file) as f:
                return f.read().strip()
        os.makedirs(os.path.dirname(self.key_file), exist_ok=True)
        key = str(uuid.uuid4())
        with open(self.key_file, "w") as f:
            f.write(key)
        log.info("Generated new system key: %s", self.key_file)
        return key

    def _load_trust(self) -> dict[str, str]:
        """Return the trust map from .github/trust.json, always including conductor."""
        trust: dict[str, str] = {"conductor": self.home}
        if os.path.exists(self.trust_file):
            try:
                with open(self.trust_file) as f:
                    trust.update(json.load(f))
            except Exception:
                pass
        # Ensure conductor entry is always correct
        trust["conductor"] = self.home
        return trust

    def _register_project_trust(self, name: str, directory: str) -> None:
        """Add or update a project entry in .github/trust.json and re-export TRUSTED_DIRECTORIES."""
        trust = self._load_trust()
        if trust.get(name) == directory:
            return
        trust[name] = directory
        os.makedirs(os.path.dirname(self.trust_file), exist_ok=True)
        with open(self.trust_file, "w") as f:
            json.dump(trust, f, indent=2)
        # Update the running env so callers see the new list immediately
        dirs = list(dict.fromkeys(trust.values()))  # preserve insertion order, deduplicate
        os.environ["TRUSTED_DIRECTORIES"] = ":".join(dirs)

    def project_dir(self, project: str) -> str:
        return os.path.join(self.home, ".github", "projects", project)

    def project_agents_dir(self, project: str) -> str:
        return os.path.join(self.project_dir(project), "agents")

    def project_retired_dir(self, project: str) -> str:
        return os.path.join(self.project_agents_dir(project), "retired")

    def project_cron_dir(self, project: str) -> str:
        return os.path.join(self.project_dir(project), "cron")

    def _role_cron_root(self, role: str, project: str | None = None) -> str:
        """Return the cron directory for a role: project-scoped or system-level."""
        scope = self.config.get("agent", {}).get(role, {}).get("scope", "system")
        if scope == "project" and project:
            return self.project_cron_dir(project)
        return self.cron_dir

    @property
    def sync_log(self) -> str:
        return os.path.join(self.home, ".github", "scripts", "sync_agents.log")

    # ── Config ─────────────────────────────────────────────────────────────────

    @property
    def config(self) -> dict:
        """Return conductor.json as a dict (lazy-loaded, cached per instance)."""
        if self._cfg is None:
            with open(self.config_path) as f:
                self._cfg = json.load(f)
        return self._cfg

    def reload_config(self) -> dict:
        """Discard the cached config and reload from disk."""
        self._cfg = None
        return self.config

    @property
    def default_model(self) -> str:
        return self.config.get("models", {}).get("default_model", "claude-opus-4.6")

    @property
    def active_roles(self) -> set[str]:
        """Return the set of active non-conductor role keys."""
        return {
            k
            for k, v in self.config.get("agent", {}).items()
            if k != "conductor" and v.get("active", False)
        }

    def _service_config(self) -> tuple[str, int]:
        """Read [con-pilot] host/port from $CONDUCTOR_HOME/.env (TOML). Falls back to localhost:8000."""
        env_path = Path(self.home) / ".env"
        if env_path.exists():
            with open(env_path, "rb") as f:
                data = tomllib.load(f)
            section = data.get("con-pilot", {})
            return str(section.get("host", "localhost")), int(section.get("port", 8000))
        return "localhost", 8000

    # ── Session environment ─────────────────────────────────────────────────────

    @property
    def env(self) -> dict[str, str]:
        """Return the session env vars derived from conductor.json and trust.json."""
        trust = self._load_trust()
        dirs = list(dict.fromkeys(trust.values()))  # ordered, deduplicated

        agents = self.config.get("agent", {})
        conductor_name = agents.get("conductor", {}).get("name", "conductor")
        project = os.environ.get("PROJECT_NAME") or None

        active = {k: v for k, v in agents.items() if k != "conductor" and v.get("active", False)}
        sidekick_roles = [k for k, v in active.items() if v.get("sidekick", False)]

        if len(sidekick_roles) > 1:
            role_cfg = active.get("developer") if "developer" in sidekick_roles else None
            raw = role_cfg.get("name", "developer") if role_cfg else conductor_name
            sidekick_name = self._expand_name(raw, project=project, rank=1)
        elif len(sidekick_roles) == 1:
            raw = active[sidekick_roles[0]].get("name", sidekick_roles[0])
            sidekick_name = self._expand_name(raw, project=project, rank=1)
        else:
            sidekick_name = conductor_name

        result: dict[str, str] = {}
        if self.home:
            result["CONDUCTOR_HOME"] = self.home
        if dirs:
            result["TRUSTED_DIRECTORIES"] = ":".join(dirs)
        if self.default_model:
            result["COPILOT_DEFAULT_MODEL"] = self.default_model
        result["CONDUCTOR_AGENT_NAME"] = conductor_name
        result["SIDEKICK_AGENT_NAME"] = sidekick_name
        return result

    # ── Project resolution ──────────────────────────────────────────────────────

    def _find_project_root(self, directory: str) -> str:
        """Walk up from directory until a .git root is found, or return directory itself."""
        d = Path(directory).resolve()
        while d != d.parent:
            if (d / ".git").exists():
                return str(d)
            d = d.parent
        return directory

    def _infer_project_name(self, directory: str) -> str | None:
        """Try to infer the project name from common project files in directory."""
        d = Path(directory)
        # pyproject.toml
        pyproject = d / "pyproject.toml"
        if pyproject.exists():
            try:
                with open(pyproject, "rb") as f:
                    data = tomllib.load(f)
                name = data.get("project", {}).get("name") or (
                    data.get("tool", {}).get("poetry", {}).get("name")
                )
                if name:
                    return name
            except Exception:
                pass
        # package.json
        pkg = d / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
                if name := data.get("name"):
                    return name
            except Exception:
                pass
        # .git/config remote URL
        git_config = d / ".git" / "config"
        if git_config.exists():
            try:
                for line in git_config.read_text().splitlines():
                    if "url = " in line:
                        url = line.split("url = ")[-1].strip()
                        name = url.rstrip("/").split("/")[-1]
                        if name.endswith(".git"):
                            name = name[:-4]
                        if name:
                            return name
            except Exception:
                pass
        # Fallback: directory basename
        return d.name or None

    def resolve_project(self, cwd: str | None = None) -> tuple[str, str] | None:
        """
        Resolve (project_name, project_directory) for the given working directory.

        Strategy:
          1. Infer from pyproject.toml / package.json / .git config / dirname.
          2. Prompt the user interactively (only when stdin is a tty).

        Sets PROJECT_NAME in the environment so child processes inherit it.
        Returns None if the project cannot be determined.
        """
        directory = self._find_project_root(cwd or os.getcwd())

        # 1. Infer from filesystem
        name = self._infer_project_name(directory)

        # 2. Ask user interactively
        if not name and sys.stdin.isatty():
            try:
                name = (
                    input(
                        f"Cannot infer project name for {directory}.\nEnter project name: "
                    ).strip()
                    or None
                )
            except (EOFError, KeyboardInterrupt):
                name = None

        if name:
            os.environ["PROJECT_NAME"] = name
            self._register_project_trust(name, directory)
            return name, directory

        log.warning("Could not determine project name for %s", directory)
        return None

    # ── Agent name expansion ───────────────────────────────────────────────────

    def _expand_name(
        self, template: str, project: str | None = None, rank: int | None = None
    ) -> str:
        """
        Expand name template placeholders.

        Substitutions:
          [scope:project] → project name (or removed if no project)
          [rank]          → instance number (or removed if no rank)
          Any remaining [placeholder] tokens are stripped.
        """
        name = template
        name = name.replace("[scope:project]", project or "")
        name = name.replace("[scope]", project or "")
        name = name.replace("[rank]", str(rank) if rank is not None else "")
        # Strip any remaining bracketed placeholders
        name = re.sub(r"\[.*?\]", "", name)
        # Collapse multiple hyphens and strip leading/trailing ones
        name = re.sub(r"-+", "-", name).strip("-")
        return name

    def start_watcher(self) -> int:
        """Spawn ``con-pilot serve`` as a detached background process. Returns its PID."""
        log_path = self.sync_log
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a") as lf:
            proc = subprocess.Popen(
                [sys.argv[0], "serve"],
                stdout=lf,
                stderr=lf,
                start_new_session=True,
            )
        return proc.pid

    def register(self, name: str, directory: str) -> None:
        """
        Register a new project: create its directory structure, update trust.json,
        and run a sync cycle to create its agent files.

        Args:
            name:      Project name (e.g. "my-app").
            directory: Absolute path to the project's root directory.
        """
        directory = str(Path(directory).resolve())

        # 1. Update trust.json
        self._register_project_trust(name, directory)
        log.info("Registered project '%s' at %s", name, directory)

        # 2. Create project directory structure
        p_agents = self.project_agents_dir(name)
        p_retired = self.project_retired_dir(name)
        p_cron = self.project_cron_dir(name)
        for d in (p_agents, p_retired, p_cron):
            os.makedirs(d, exist_ok=True)
        log.info("Created project directories under %s", self.project_dir(name))

        # 3. Sync agents for this project
        os.environ["PROJECT_NAME"] = name
        self.sync(cwd=directory)
        log.info("Agent sync complete for project '%s'", name)

    def retire_project(self, name: str) -> None:
        """
        Retire a project: move its directory from .github/projects/{name} to
        .github/retired-projects/{name} and remove it from trust.json.

        Args:
            name: Project name to retire.
        """
        src = self.project_dir(name)
        retired_root = os.path.join(self.home, ".github", "retired-projects")
        dst = os.path.join(retired_root, name)

        if not os.path.exists(src):
            log.warning("Project directory not found, nothing to move: %s", src)
        else:
            os.makedirs(retired_root, exist_ok=True)
            if os.path.exists(dst):
                # Suffix with timestamp to avoid collision
                from datetime import datetime  # noqa: PLC0415

                stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                dst = f"{dst}.{stamp}"
            shutil.move(src, dst)
            log.info("Moved %s -> %s", src, dst)

        # Remove from trust.json
        trust = self._load_trust()
        if name in trust:
            del trust[name]
            # Always keep conductor entry correct
            trust["conductor"] = self.home
            os.makedirs(os.path.dirname(self.trust_file), exist_ok=True)
            with open(self.trust_file, "w") as f:
                json.dump(trust, f, indent=2)
            dirs = list(dict.fromkeys(trust.values()))
            os.environ["TRUSTED_DIRECTORIES"] = ":".join(dirs)
            log.info("Removed '%s' from trust.json", name)
        else:
            log.warning("Project '%s' was not in trust.json", name)

        log.info("Project '%s' retired", name)

    # ── Agent instruction editing ───────────────────────────────────────────────

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[str, str]:
        """
        Split an agent file into ``(frontmatter_block, body)``.

        ``frontmatter_block`` includes the surrounding ``---`` delimiters.
        ``body`` is everything after the closing ``---``.
        """
        if content.startswith("---"):
            end = content.index("---", 3)
            fm = content[: end + 3]
            body = content[end + 3 :]
            return fm, body
        return "", content

    def _resolve_agent_files(self, role: str, project: str | None) -> list[str]:
        """
        Return the list of existing .agent.md file paths for a given role.

        For system agents (or when project is None) looks in .github/agents/.
        For project agents looks in .github/projects/{project}/agents/.
        """
        scope = self.config.get("agent", {}).get(role, {}).get("scope", "system")
        if scope == "project" and project:
            search_dir = self.project_agents_dir(project)
            # Match role.project.agent.md and role.project.N.agent.md
            prefix = f"{role}.{project}."
        else:
            search_dir = self.agents_dir
            prefix = f"{role}."

        if not os.path.isdir(search_dir):
            return []
        return sorted(
            os.path.join(search_dir, f)
            for f in os.listdir(search_dir)
            if f.startswith(prefix) and f.endswith(".agent.md")
        )

    def _check_system_key(self, role: str, key: str | None) -> None:
        """
        Raise ValueError if a system agent (non-conductor) is being edited without
        the correct system key.  Conductor is always blocked.
        """
        if role == "conductor":
            raise ValueError("The conductor agent cannot be updated via con-pilot.")
        scope = self.config.get("agent", {}).get(role, {}).get("scope", "system")
        if scope != "project":
            expected = self._load_or_generate_key()
            if key != expected:
                raise ValueError(
                    f"System agent '{role}' requires the correct system key. " "Pass it with --key."
                )

    def amend_agent(
        self, instructions_file: str, role: str, project: str | None = None, key: str | None = None
    ) -> None:
        """
        Amend agent(s) of ``role`` by appending / merging an ``## Instructions``
        section from ``instructions_file``.  Existing ``## Instructions`` content
        is replaced; all other sections are preserved.
        """
        self._check_system_key(role, key)
        files = self._resolve_agent_files(role, project)
        if not files:
            raise FileNotFoundError(
                f"No agent files found for role='{role}'"
                + (f" project='{project}'" if project else "")
            )
        new_instructions = Path(instructions_file).read_text().strip()
        for fpath in files:
            content = Path(fpath).read_text()
            fm, body = self._split_frontmatter(content)
            # Remove existing ## Instructions block if present
            body = re.sub(r"\n## Instructions\b.*?(?=\n## |\Z)", "", body, flags=re.DOTALL)
            body = body.rstrip() + f"\n\n## Instructions\n{new_instructions}\n"
            Path(fpath).write_text(fm + body)
            log.info("Amended: %s", fpath)

    def replace_agent(
        self, instructions_file: str, role: str, project: str | None = None, key: str | None = None
    ) -> None:
        """
        Replace the body of agent(s) of ``role`` entirely with the content of
        ``instructions_file``, keeping the YAML frontmatter intact.
        """
        self._check_system_key(role, key)
        files = self._resolve_agent_files(role, project)
        if not files:
            raise FileNotFoundError(
                f"No agent files found for role='{role}'"
                + (f" project='{project}'" if project else "")
            )
        new_body = "\n" + Path(instructions_file).read_text().strip() + "\n"
        for fpath in files:
            content = Path(fpath).read_text()
            fm, _ = self._split_frontmatter(content)
            Path(fpath).write_text(fm + new_body)
            log.info("Replaced: %s", fpath)

    def reset_agent(self, role: str, project: str | None = None, key: str | None = None) -> None:
        """
        Reset agent(s) of ``role`` to the template (or re-generate from conductor.json
        if no template exists).  Existing files are overwritten.
        """
        self._check_system_key(role, key)
        files = self._resolve_agent_files(role, project)
        if not files:
            raise FileNotFoundError(
                f"No agent files found for role='{role}'"
                + (f" project='{project}'" if project else "")
            )
        role_cfg = self.config.get("agent", {}).get(role, {})
        name_tmpl = role_cfg.get("name", role)
        max_inst = role_cfg.get("instances", {}).get("max")

        for fpath in files:
            fname = os.path.basename(fpath)
            # Determine rank for numbered instances
            rank: int | None = None
            if max_inst:
                m = re.search(r"\.(\d+)\.agent\.md$", fname)
                rank = int(m.group(1)) if m else None
            expanded = self._expand_name(name_tmpl, project=project, rank=rank)
            content = self._generate_agent_file(
                role, {**role_cfg, "name": expanded}, self.default_model
            )
            Path(fpath).write_text(content)
            log.info("Reset: %s", fpath)

    def print_env(self, shell: bool = False) -> None:
        """
        Print session env vars to stdout, resolve project context, start the watcher.

        Outputs all env vars plus ``PROJECT_NAME`` (if resolved) and ``SYNC_AGENTS_PID``.
        """
        # Resolve project first so PROJECT_NAME is in os.environ when self.env reads it
        result = self.resolve_project()
        if result:
            os.environ["PROJECT_NAME"] = result[0]
        env = self.env
        if result:
            env["PROJECT_NAME"] = result[0]
        pid = self.start_watcher()
        env["SYNC_AGENTS_PID"] = str(pid)
        for key, value in env.items():
            if shell:
                print(f'export {key}="{value}"')
            else:
                print(f"{key}={value}")

    # ── Agent sync ─────────────────────────────────────────────────────────────

    def _apply_template(self, template: str, name: str, model: str) -> str:
        """
        Adapt a template file for a new agent instance.

        Replaces the ``name:`` and ``model:`` lines in the YAML frontmatter and
        updates the first ``You are **…**`` line in the body.  Everything else
        (description, tools, ## sections) is preserved verbatim.
        """
        import re as _re

        # Replace name: and model: in frontmatter
        result = _re.sub(r'(?m)^name: ".*?"', f'name: "{name}"', template)
        result = _re.sub(r'(?m)^model: ".*?"', f'model: "{model}"', result)
        # Update the intro line "You are **old-name**,"
        result = _re.sub(r"(?m)^(You are \*\*).*?(\*\*,)", rf"\g<1>{name}\2", result)
        return result

    def _generate_agent_file(self, role: str, cfg: dict, model: str) -> str:
        """Return the Markdown content for a .agent.md file.

        If a template exists at ``.github/agents/templates/{role}.agent.md``,
        it is used as the base and adapted for ``name`` and ``model``.
        Otherwise content is generated from the conductor.json description.
        """
        name = cfg.get("name", role)

        # Use template if available
        template_path = os.path.join(self.templates_dir, f"{role}.agent.md")
        if os.path.exists(template_path):
            with open(template_path) as f:
                return self._apply_template(f.read(), name, model)

        description = cfg.get("description", "")
        sidekick_note = (
            "\n\n## Sidekick\n"
            "You are the designated **sidekick** — always available to assist "
            "with development tasks without needing to be explicitly invoked."
            if cfg.get("sidekick", False)
            else ""
        )
        if description:
            return textwrap.dedent(f"""\
                ---
                name: "{name}"
                description: "Use when: {description}"
                model: "{model}"
                tools: [read, edit, search, execute, agent, todo, web]
                ---

                You are **{name}**, the {role} agent for this system.

                ## Role
                {description}{sidekick_note}

                ## Behavior
                - Follow the session setup defined in `$CONDUCTOR_HOME/.github/copilot-instructions.md`
                - Respect `TRUSTED_DIRECTORIES` — do not operate outside them without explicit user confirmation
                - Use the model specified in `COPILOT_DEFAULT_MODEL` unless the user overrides it
                - For destructive or irreversible actions, always ask before proceeding
            """)
        return textwrap.dedent(f"""\
            ---
            name: "{name}"
            model: "{model}"
            ---
        """)

    def _ensure_system_agents(self) -> None:
        """Ensure conductor.agent.md and all scope=system agents exist at startup."""
        os.makedirs(self.agents_dir, exist_ok=True)
        os.makedirs(self.retired_dir, exist_ok=True)

        agent_cfg = self.config.get("agent", {})

        # Build list: conductor first, then other active system-scoped roles
        system_roles = ["conductor"] + [
            role
            for role, cfg in agent_cfg.items()
            if role != "conductor"
            and cfg.get("active", False)
            and cfg.get("scope", "system") != "project"
        ]

        for role in system_roles:
            fname = f"{role}.agent.md"
            dest = os.path.join(self.agents_dir, fname)
            if os.path.exists(dest):
                continue
            retired = os.path.join(self.retired_dir, fname)
            if os.path.exists(retired):
                shutil.move(retired, dest)
                log.info("Restored (system): %s", fname)
                continue
            cfg = agent_cfg.get(role, {})
            content = self._generate_agent_file(
                role,
                {**cfg, "name": self._expand_name(cfg.get("name", role))},
                self.default_model,
            )
            with open(dest, "w") as f:
                f.write(content)
            log.info("Created (system): %s", fname)

    def sync(self, cwd: str | None = None) -> None:
        """
        Reconcile .agent.md files against conductor.json, then dispatch cron jobs.

        System agents (scope=system or no scope) are written to .github/agents/.
        Project agents (scope=project) are written to
        .github/projects/{project}/agents/, with numbered sub-files when
        instances.max is set.

        The project is resolved from ``cwd`` → env var ``PROJECT_NAME`` →
        filesystem inference → interactive prompt.
        """
        cfg = self.reload_config()
        roles = self.active_roles
        agent_cfg = cfg.get("agent", {})

        os.makedirs(self.agents_dir, exist_ok=True)
        os.makedirs(self.retired_dir, exist_ok=True)

        # Resolve project once for all project-scoped agents
        project_name: str | None = os.environ.get("PROJECT_NAME") or None
        if not project_name:
            result = self.resolve_project(cwd)
            project_name = result[0] if result else None

        # Split roles by scope
        system_roles = [
            r for r in roles if agent_cfg.get(r, {}).get("scope", "system") != "project"
        ]
        project_roles = [
            r for r in roles if agent_cfg.get(r, {}).get("scope", "system") == "project"
        ]

        # ── System agents in .github/agents/ ───────────────────────────────────
        expected_system: set[str] = {"conductor.agent.md"}
        for role in system_roles:
            expected_system.add(f"{role}.agent.md")

        for fname in os.listdir(self.agents_dir):
            if not fname.endswith(".agent.md") or fname == "conductor.agent.md":
                continue
            if fname not in expected_system:
                shutil.move(
                    os.path.join(self.agents_dir, fname),
                    os.path.join(self.retired_dir, fname),
                )
                log.info("Retired (system): %s", fname)

        for role in system_roles:
            fname = f"{role}.agent.md"
            dest = os.path.join(self.agents_dir, fname)
            if os.path.exists(dest):
                continue
            retired = os.path.join(self.retired_dir, fname)
            if os.path.exists(retired):
                shutil.move(retired, dest)
                log.info("Restored (system): %s", fname)
            else:
                content = self._generate_agent_file(
                    role,
                    {
                        **agent_cfg.get(role, {}),
                        "name": self._expand_name(agent_cfg.get(role, {}).get("name", role)),
                    },
                    self.default_model,
                )
                with open(dest, "w") as f:
                    f.write(content)
                log.info("Created (system): %s", fname)

        # ── Project agents in .github/projects/{project}/agents/ ───────────────
        if project_roles and project_name:
            p_agents = self.project_agents_dir(project_name)
            p_retired = self.project_retired_dir(project_name)
            os.makedirs(p_agents, exist_ok=True)
            os.makedirs(p_retired, exist_ok=True)

            expected_project: set[str] = set()
            for role in project_roles:
                max_inst = agent_cfg.get(role, {}).get("instances", {}).get("max")
                if max_inst:
                    for i in range(1, max_inst + 1):
                        expected_project.add(f"{role}.{project_name}.{i}.agent.md")
                else:
                    expected_project.add(f"{role}.{project_name}.agent.md")

            for fname in os.listdir(p_agents):
                if not fname.endswith(".agent.md"):
                    continue
                if fname not in expected_project:
                    shutil.move(
                        os.path.join(p_agents, fname),
                        os.path.join(p_retired, fname),
                    )
                    log.info("Retired (project=%s): %s", project_name, fname)

            for role in project_roles:
                role_cfg = agent_cfg.get(role, {})
                name_tmpl = role_cfg.get("name", role)
                max_inst = role_cfg.get("instances", {}).get("max")

                if max_inst:
                    for i in range(1, max_inst + 1):
                        fname = f"{role}.{project_name}.{i}.agent.md"
                        dest = os.path.join(p_agents, fname)
                        if os.path.exists(dest):
                            continue
                        retired = os.path.join(p_retired, fname)
                        if os.path.exists(retired):
                            shutil.move(retired, dest)
                            log.info("Restored (project=%s): %s", project_name, fname)
                        else:
                            expanded = self._expand_name(name_tmpl, project=project_name, rank=i)
                            content = self._generate_agent_file(
                                role, {**role_cfg, "name": expanded}, self.default_model
                            )
                            with open(dest, "w") as f:
                                f.write(content)
                            log.info("Created (project=%s): %s rank=%d", project_name, fname, i)
                else:
                    fname = f"{role}.{project_name}.agent.md"
                    dest = os.path.join(p_agents, fname)
                    if os.path.exists(dest):
                        continue
                    retired = os.path.join(p_retired, fname)
                    if os.path.exists(retired):
                        shutil.move(retired, dest)
                        log.info("Restored (project=%s): %s", project_name, fname)
                    else:
                        expanded = self._expand_name(name_tmpl, project=project_name)
                        content = self._generate_agent_file(
                            role, {**role_cfg, "name": expanded}, self.default_model
                        )
                        with open(dest, "w") as f:
                            f.write(content)
                        log.info("Created (project=%s): %s", project_name, fname)

        self.cron(project=project_name)

    # ── Cron ───────────────────────────────────────────────────────────────────

    def _cron_state_path(self, role: str, job_name: str, project: str | None = None) -> str:
        root = self._role_cron_root(role, project)
        return os.path.join(root, ".state", f"{role}__{job_name}.last_run")

    def _last_run(self, role: str, job_name: str, project: str | None = None) -> datetime:
        path = self._cron_state_path(role, job_name, project)
        if not os.path.exists(path):
            return datetime.fromtimestamp(0, tz=UTC)
        with open(path) as f:
            return datetime.fromisoformat(f.read().strip())

    def _save_last_run(
        self, role: str, job_name: str, dt: datetime, project: str | None = None
    ) -> None:
        state_dir = os.path.join(self._role_cron_root(role, project), ".state")
        os.makedirs(state_dir, exist_ok=True)
        with open(self._cron_state_path(role, job_name, project), "w") as f:
            f.write(dt.isoformat())

    def _cron_is_due(self, schedule: str, last_run: datetime) -> bool:
        now = datetime.now(tz=UTC)
        if not HAS_CRONITER:
            return (now - last_run).total_seconds() >= 86400
        cron = croniter(schedule, last_run.replace(tzinfo=None))
        next_run = cron.get_next(datetime).replace(tzinfo=UTC)
        return now >= next_run

    def _append_pending(
        self,
        role: str,
        agent_name: str,
        job_name: str,
        task: str,
        schedule: str,
        *,
        project: str | None = None,
    ) -> None:
        cron_dir = self._role_cron_root(role, project)
        os.makedirs(cron_dir, exist_ok=True)
        pending_log = os.path.join(cron_dir, "pending.log")
        now = datetime.now(tz=UTC).isoformat()
        with open(pending_log, "a") as f:
            f.write(
                f"[{now}] role={role} agent={agent_name} job={job_name} "
                f"schedule={schedule!r}\n  task: {task}\n\n"
            )

    def cron(self, project: str | None = None) -> None:
        """Check all agents with has_cron_jobs=true and queue any due tasks."""
        # Ensure system cron dirs exist
        os.makedirs(self.cron_dir, exist_ok=True)
        os.makedirs(self.cron_state_dir, exist_ok=True)

        for role, role_cfg in self.config.get("agent", {}).items():
            if not role_cfg.get("has_cron_jobs", False):
                continue
            if not role_cfg.get("active", role == "conductor"):
                continue

            scope = role_cfg.get("scope", "system")
            role_project = project if scope == "project" else None
            cron_dir = self._role_cron_root(role, role_project)
            os.makedirs(cron_dir, exist_ok=True)
            os.makedirs(os.path.join(cron_dir, ".state"), exist_ok=True)

            cron_file = os.path.join(cron_dir, f"{role}.cron")
            if not os.path.exists(cron_file):
                with open(cron_file, "w") as f:
                    f.write(_CRON_PLACEHOLDER.format(role=role))
                log.info("Created placeholder cron file: %s.cron", role)
                continue

            try:
                with open(cron_file, "rb") as f:
                    cron_cfg = tomllib.load(f)
            except Exception as exc:
                log.warning("Failed to parse %s.cron: %s", role, exc)
                continue

            agent_name = role_cfg.get("name", role)
            for job in cron_cfg.get("job", []):
                job_name = job.get("name", "unnamed")
                schedule = job.get("schedule", "")
                task = job.get("task", "")
                if not schedule or not task:
                    continue
                last = self._last_run(role, job_name, role_project)
                if self._cron_is_due(schedule, last):
                    now = datetime.now(tz=UTC)
                    self._append_pending(
                        role, agent_name, job_name, task, schedule, project=role_project
                    )
                    self._save_last_run(role, job_name, now, role_project)
                    log.info("Queued: [%s] %s — %s...", role, job_name, task[:60])

    # ── HTTP service ────────────────────────────────────────────────────────────

    def create_app(self, interval: int | None = None):
        """
        Build and return the FastAPI application.

        Starts a background thread that calls ``self.sync()`` on every cycle.
        """
        from fastapi import FastAPI  # noqa: PLC0415

        cycle = interval if interval is not None else self.DEFAULT_INTERVAL

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            self._ensure_system_agents()

            def _loop() -> None:
                while True:
                    try:
                        self.sync()
                    except Exception:
                        log.exception("Sync cycle failed")
                    log.info("Next sync in %ds", cycle)
                    time.sleep(cycle)

            t = threading.Thread(target=_loop, daemon=True)
            t.start()
            log.info("con-pilot background sync started (interval=%ds)", cycle)
            yield

        app = FastAPI(title="con-pilot", lifespan=lifespan)

        @app.get("/health")
        def health() -> dict:
            return {"status": "ok"}

        @app.post("/sync")
        def sync_route() -> dict:
            self.sync()
            return {"status": "ok"}

        @app.post("/cron")
        def cron_route() -> dict:
            self.cron()
            return {"status": "ok"}

        @app.get("/setup-env")
        def setup_env_route() -> dict:
            result = self.resolve_project()
            if result:
                os.environ["PROJECT_NAME"] = result[0]
            env = dict(self.env)
            if result:
                env["PROJECT_NAME"] = result[0]
            return env

        @app.post("/register")
        def register_route(body: dict) -> dict:
            self.register(body["name"], body["directory"])
            return {"status": "ok"}

        @app.post("/retire-project")
        def retire_project_route(body: dict) -> dict:
            self.retire_project(body["name"])
            return {"status": "ok"}

        # amend route disabled — pending implementation
        # @app.post("/amend")
        # def amend_route(body: dict) -> dict:
        #     self.amend_agent(
        #         body["file"], body["role"], body.get("project"), body.get("key")
        #     )
        #     return {"status": "ok"}

        @app.post("/replace")
        def replace_route(body: dict) -> dict:
            self.replace_agent(
                body["file"], body["role"], body.get("project"), body.get("key")
            )
            return {"status": "ok"}

        @app.post("/reset")
        def reset_route(body: dict) -> dict:
            self.reset_agent(
                body["role"], body.get("project"), body.get("key")
            )
            return {"status": "ok"}

        return app

    def serve(self, interval: int | None = None) -> None:
        """Start the con-pilot FastAPI service via uvicorn."""
        import uvicorn  # noqa: PLC0415

        cycle = interval if interval is not None else self.DEFAULT_INTERVAL
        host, port = self._service_config()
        app = self.create_app(interval=cycle)
        log.info(
            "Starting con-pilot API (host=%s, port=%d, interval=%ds, home=%s)",
            host,
            port,
            cycle,
            self.home,
        )
        uvicorn.run(app, host=host, port=port)
