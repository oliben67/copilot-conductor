"""
conductor.py — ConPilot: the single facade for all con-pilot operations.

All functionality (bootstrap, config, agent sync, cron dispatch,
session-env resolution, watcher management, and HTTP service) lives here.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import yaml

from con_pilot.auth import (
    GitHubToken,
    resolve_github_token,
)
from con_pilot.models import (
    AgentInfo,
    AgentListResponse,
    ConductorConfig,
    ValidationError,
    ValidationResult,
)
from con_pilot.paths import PathResolver
from con_pilot.trust import TrustRegistry

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

    def __init__(
        self, conductor_home: str | None = None, *, require_token: bool = True
    ) -> None:
        from con_pilot.core.services.config_store import ConfigStore  # noqa: PLC0415
        from con_pilot.core.services.snapshot import SnapshotService  # noqa: PLC0415

        # Validate GitHub token first — fail fast if conflicting or missing
        self._token: GitHubToken | None = resolve_github_token(required=require_token)

        self._paths = PathResolver(conductor_home)
        self._trust = TrustRegistry(self._paths)
        self._cfg: ConductorConfig | None = None
        self._config_store = ConfigStore(self._paths)
        self._snapshot_service = SnapshotService(self._paths)

    @property
    def config_store(self):
        """ConfigStore for managing configuration versions."""
        return self._config_store

    @property
    def snapshot_service(self):
        """SnapshotService for managing .github directory snapshots."""
        return self._snapshot_service

    @property
    def github_token(self) -> GitHubToken | None:
        """Resolved GitHub token (from COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN)."""
        return self._token

    @property
    def home(self) -> str:
        """CONDUCTOR_HOME directory (delegated to PathResolver)."""
        return self._paths.home

    # ── Paths (delegated to PathResolver) ──────────────────────────────────────

    @property
    def config_path(self) -> str:
        return self._paths.config_path

    @property
    def agents_dir(self) -> str:
        return self._paths.agents_dir

    @property
    def retired_dir(self) -> str:
        return self._paths.retired_dir

    @property
    def cron_dir(self) -> str:
        return self._paths.cron_dir

    @property
    def cron_state_dir(self) -> str:
        return self._paths.cron_state_dir

    @property
    def pending_log(self) -> str:
        return self._paths.pending_log

    @property
    def templates_dir(self) -> str:
        return self._paths.templates_dir

    @property
    def trust_file(self) -> str:
        return self._paths.trust_file

    @property
    def key_file(self) -> str:
        return self._paths.key_file

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
        os.chmod(self.key_file, 0o600)
        log.info("Generated new system key: %s", self.key_file)
        return key

    # ── Trust (delegated to TrustRegistry) ─────────────────────────────────────

    def _load_trust(self) -> dict[str, str]:
        """Return the trust map from .github/trust.json, always including conductor."""
        return self._trust.load()

    def _register_project_trust(self, name: str, directory: str) -> None:
        """Add or update a project entry in .github/trust.json and re-export TRUSTED_DIRECTORIES."""
        self._trust.register(name, directory)

    # ── Project paths (delegated to PathResolver) ──────────────────────────────

    def project_dir(self, project: str) -> str:
        return self._paths.project_dir(project)

    def project_agents_dir(self, project: str) -> str:
        return self._paths.project_agents_dir(project)

    def project_retired_dir(self, project: str) -> str:
        return self._paths.project_retired_dir(project)

    def project_cron_dir(self, project: str) -> str:
        return self._paths.project_cron_dir(project)

    def _role_cron_root(self, role: str, project: str | None = None) -> str:
        """Return the cron directory for a role: project-scoped or system-level."""
        scope = self.config.get_agent_dict(role).get("scope", "system")
        if scope == "project" and project:
            return self.project_cron_dir(project)
        return self.cron_dir

    @property
    def sync_log(self) -> str:
        return self._paths.sync_log

    # ── Config ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _load_config_file(path: str) -> dict:
        """
        Load config from path, detecting format by extension.

        Supports both YAML (.yaml, .yml) and JSON (.json) files.
        """
        with open(path) as f:
            if path.endswith((".yaml", ".yml")):
                return yaml.safe_load(f)
            return json.load(f)

    @property
    def config(self) -> ConductorConfig:
        """Return conductor config (lazy-loaded, cached per instance)."""
        if self._cfg is None:
            data = self._load_config_file(self.config_path)
            self._cfg = ConductorConfig(**data)
        return self._cfg

    def reload_config(self) -> ConductorConfig:
        """Discard the cached config and reload from disk."""
        self._cfg = None
        return self.config

    @property
    def schema_path(self) -> str:
        """Return the path to the conductor.json schema file."""
        return self._paths.schema_path

    def validate(self, config_path: str | None = None) -> ValidationResult:
        """
        Validate conductor config against the JSON schema.

        Args:
            config_path: Path to config file to validate. Defaults to self.config_path.
                         Supports both YAML (.yaml, .yml) and JSON (.json) files.

        Returns:
            ValidationResult with valid=True if valid, or list of errors if invalid.
        """
        from jsonschema import Draft202012Validator  # noqa: PLC0415

        target_path = config_path or self.config_path
        errors: list[ValidationError] = []
        warnings: list[str] = []

        # Check if config file exists
        if not os.path.exists(target_path):
            return ValidationResult(
                valid=False,
                errors=[
                    ValidationError(
                        path="$",
                        message=f"Configuration file not found: {target_path}",
                        validator="file_exists",
                    )
                ],
                config_path=target_path,
                schema_path=None,
            )

        # Load config (supports YAML and JSON)
        try:
            config_data = self._load_config_file(target_path)
        except yaml.YAMLError as e:
            return ValidationResult(
                valid=False,
                errors=[
                    ValidationError(
                        path="$",
                        message=f"Invalid YAML: {e}",
                        validator="yaml_parse",
                    )
                ],
                config_path=target_path,
                schema_path=None,
            )
        except json.JSONDecodeError as e:
            return ValidationResult(
                valid=False,
                errors=[
                    ValidationError(
                        path="$",
                        message=f"Invalid JSON: {e}",
                        validator="json_parse",
                    )
                ],
                config_path=target_path,
                schema_path=None,
            )

        # Load schema
        schema_file = self.schema_path
        if not os.path.exists(schema_file):
            return ValidationResult(
                valid=False,
                errors=[
                    ValidationError(
                        path="$",
                        message=f"Schema file not found: {schema_file}",
                        validator="schema_exists",
                    )
                ],
                config_path=target_path,
                schema_path=None,
            )

        try:
            with open(schema_file) as f:
                schema = json.load(f)
        except json.JSONDecodeError as e:
            return ValidationResult(
                valid=False,
                errors=[
                    ValidationError(
                        path="$",
                        message=f"Invalid schema JSON: {e}",
                        validator="schema_parse",
                    )
                ],
                config_path=target_path,
                schema_path=schema_file,
            )

        # Validate against schema
        validator = Draft202012Validator(schema)
        for error in sorted(
            validator.iter_errors(config_data), key=lambda e: str(e.path)
        ):
            path = (
                "$." + ".".join(str(p) for p in error.absolute_path)
                if error.absolute_path
                else "$"
            )
            errors.append(
                ValidationError(
                    path=path,
                    message=error.message,
                    validator=error.validator,
                )
            )

        # Additional semantic validations
        if not errors:
            # Check default_model is in authorized_models
            models = config_data.get("models", {})
            default = models.get("default_model")
            authorized = models.get("authorized_models", [])
            if default and default not in authorized:
                warnings.append(
                    f"default_model '{default}' is not in authorized_models list"
                )

            # Check for multiple sidekick agents
            agents = config_data.get("agent", {})
            sidekicks = [role for role, cfg in agents.items() if cfg.get("sidekick")]
            if len(sidekicks) > 1:
                warnings.append(
                    f"Multiple agents marked as sidekick: {', '.join(sidekicks)}. "
                    "Only one agent should have sidekick=true."
                )

            # Check agent model references
            for role, cfg in agents.items():
                model = cfg.get("model")
                if model and model not in authorized:
                    warnings.append(
                        f"Agent '{role}' uses model '{model}' not in authorized_models"
                    )

        return ValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            config_path=target_path,
            schema_path=schema_file,
        )

    @property
    def default_model(self) -> str:
        return self.config.models.default_model

    @property
    def active_roles(self) -> set[str]:
        """Return the set of active non-conductor role keys."""
        return {
            k for k, v in self.config.agents.items() if k != "conductor" and v.active
        }

    def list_agents(self, project: str | None = None) -> AgentListResponse:
        """
        List all agents defined in conductor.json with their status.

        Args:
            project: Optional project name to filter project-scoped agents.
                     If None, lists all registered projects.

        Returns:
            AgentListResponse with system_agents and project_agents lists.
        """
        cfg = self.config
        system_agents: list[AgentInfo] = []
        project_agents: list[AgentInfo] = []

        # Get all registered projects from trust.json
        trust = self._load_trust()
        registered_projects = [p for p in trust.keys() if p != "conductor"]

        for role, agent_cfg in cfg.agents.items():
            scope = agent_cfg.scope
            instances = agent_cfg.instances
            max_inst = instances.max if instances else None

            if scope == "system":
                # System agent - single file in .github/agents/
                fname = f"{role}.agent.md"
                fpath = os.path.join(self.agents_dir, fname)
                exists = os.path.exists(fpath)

                system_agents.append(
                    AgentInfo(
                        role=role,
                        name=self._expand_name(agent_cfg.name or role),
                        scope="system",
                        active=agent_cfg.active,
                        file_exists=exists,
                        file_path=fpath if exists else None,
                        sidekick=agent_cfg.sidekick,
                        augmenting=agent_cfg.augmenting,
                        model=agent_cfg.model,
                        description=agent_cfg.description,
                    )
                )
            else:
                # Project-scoped agent - check each project
                projects_to_check = [project] if project else registered_projects
                for proj in projects_to_check:
                    if not proj:
                        continue
                    p_agents_dir = self.project_agents_dir(proj)

                    if max_inst and max_inst > 1:
                        # Multi-instance agent
                        for i in range(1, max_inst + 1):
                            fname = f"{role}.{proj}.{i}.agent.md"
                            fpath = os.path.join(p_agents_dir, fname)
                            exists = os.path.exists(fpath)
                            expanded_name = self._expand_name(
                                agent_cfg.name or role, project=proj, rank=i
                            )
                            project_agents.append(
                                AgentInfo(
                                    role=role,
                                    name=expanded_name,
                                    scope="project",
                                    active=agent_cfg.active,
                                    file_exists=exists,
                                    file_path=fpath if exists else None,
                                    project=proj,
                                    instance=i,
                                    sidekick=agent_cfg.sidekick,
                                    augmenting=agent_cfg.augmenting,
                                    model=agent_cfg.model,
                                    description=agent_cfg.description,
                                )
                            )
                    else:
                        # Single-instance project agent
                        fname = f"{role}.{proj}.agent.md"
                        fpath = os.path.join(p_agents_dir, fname)
                        exists = os.path.exists(fpath)
                        expanded_name = self._expand_name(
                            agent_cfg.name or role, project=proj
                        )
                        project_agents.append(
                            AgentInfo(
                                role=role,
                                name=expanded_name,
                                scope="project",
                                active=agent_cfg.active,
                                file_exists=exists,
                                file_path=fpath if exists else None,
                                project=proj,
                                sidekick=agent_cfg.sidekick,
                                augmenting=agent_cfg.augmenting,
                                model=agent_cfg.model,
                                description=agent_cfg.description,
                            )
                        )

        return AgentListResponse(
            system_agents=system_agents,
            project_agents=project_agents,
        )

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

        agents = self.config.agents
        conductor_name = agents["conductor"].name
        project = os.environ.get("PROJECT_NAME") or None

        active = {k: v for k, v in agents.items() if k != "conductor" and v.active}
        sidekick_roles = [k for k, v in active.items() if v.sidekick]

        if len(sidekick_roles) > 1:
            role_cfg = (
                active.get("developer") if "developer" in sidekick_roles else None
            )
            raw = role_cfg.name if role_cfg else conductor_name
            sidekick_name = self._expand_name(raw, project=project, rank=1)
        elif len(sidekick_roles) == 1:
            raw = (
                active[sidekick_roles[0]].name
                if active[sidekick_roles[0]].name
                else sidekick_roles[0]
            )
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
        if conductor_name is None or conductor_name.strip() == "":
            log.warning(
                "Agent names cannot be empty. Check your conductor.json configuration."
            )
            conductor_name = "conductor"  # fallback to default
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
        scope = self.config.get_agent_dict(role).get("scope", "system")
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
        scope = self.config.get_agent_dict(role).get("scope", "system")
        if scope != "project":
            expected = self._load_or_generate_key()
            if key != expected:
                raise ValueError(
                    f"System agent '{role}' requires the correct system key. "
                    "Pass it with --key."
                )

    def amend_agent(
        self,
        instructions_file: str,
        role: str,
        project: str | None = None,
        key: str | None = None,
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
            body = re.sub(
                r"\n## Instructions\b.*?(?=\n## |\Z)", "", body, flags=re.DOTALL
            )
            body = body.rstrip() + f"\n\n## Instructions\n{new_instructions}\n"
            Path(fpath).write_text(fm + body)
            log.info("Amended: %s", fpath)

    def replace_agent(
        self,
        instructions_file: str,
        role: str,
        project: str | None = None,
        key: str | None = None,
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

    def reset_agent(
        self, role: str, project: str | None = None, key: str | None = None
    ) -> None:
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
        role_cfg = self.config.get_agent_dict(role)
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

        agent_cfg = self.config.agent_dicts

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
        agent_cfg = cfg.agent_dicts

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
                        "name": self._expand_name(
                            agent_cfg.get(role, {}).get("name", role)
                        ),
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
                            expanded = self._expand_name(
                                name_tmpl, project=project_name, rank=i
                            )
                            content = self._generate_agent_file(
                                role, {**role_cfg, "name": expanded}, self.default_model
                            )
                            with open(dest, "w") as f:
                                f.write(content)
                            log.info(
                                "Created (project=%s): %s rank=%d",
                                project_name,
                                fname,
                                i,
                            )
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

    def _cron_state_path(
        self, role: str, job_name: str, project: str | None = None
    ) -> str:
        root = self._role_cron_root(role, project)
        return os.path.join(root, ".state", f"{role}__{job_name}.last_run")

    def _last_run(
        self, role: str, job_name: str, project: str | None = None
    ) -> datetime:
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
        cron = croniter(schedule, last_run.replace(tzinfo=None))  # type: ignore
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
        """Check all agents with cron config and queue any due tasks."""
        # Ensure system cron dirs exist
        os.makedirs(self.cron_dir, exist_ok=True)
        os.makedirs(self.cron_state_dir, exist_ok=True)

        for role, role_cfg in self.config.agent_dicts.items():
            cron_cfg_raw = role_cfg.get("cron")
            if not cron_cfg_raw:
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

        Delegates to the v1 API module which handles routers and lifespan.
        """
        from con_pilot.v1.api import create_app as _create_app  # noqa: PLC0415

        return _create_app(self, interval=interval)

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
