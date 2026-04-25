"""
conductor.py — ConPilot: the single facade for all con-pilot operations.

All functionality (bootstrap, config, agent sync, cron dispatch,
session-env resolution, watcher management, and HTTP service) lives here.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import tomllib
from datetime import UTC, timezone
from pathlib import Path
from typing import Any
import yaml
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from con_pilot.security.auth import (
    GitHubToken,
    resolve_github_token,
)
from con_pilot.core.models import (
    AgentDetailResponse,
    AgentListResponse,
    Conductor,
    ConductorConfig,
    ValidationError,
    ValidationResult,
)
from con_pilot.core.paths import PathResolver
from con_pilot.runtime.logger import app_logger
from con_pilot.core.trust import TrustRegistry
from con_pilot._facets.agents import AgentsFacet
from con_pilot._facets.cron import CronFacet

log = app_logger.bind(module=__name__)

class ConPilot(AgentsFacet, CronFacet):
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
    SCHEDULER_DB_FILENAME: str = "cron-jobs.sqlite3"

    def __init__(
        self, conductor_home: str | None = None, *, require_token: bool = True
    ) -> None:
        from con_pilot.core.services.config_store import ConfigStore
        from con_pilot.core.services.snapshot import SnapshotService

        # Validate GitHub token first — fail fast if conflicting or missing
        self._token: GitHubToken | None = resolve_github_token(required=require_token)

        self._paths = PathResolver(conductor_home)
        self._trust = TrustRegistry(self._paths)
        self._cfg: Conductor | None = None
        self._scheduler: AsyncIOScheduler | None = None
        self._scheduler_lock = threading.RLock()
        self._dispatcher: Any = None
        self._config_store = ConfigStore(self._paths)
        self._snapshot_service = SnapshotService(self._paths)

        # ── Service facades ────────────────────────────────────────────────
        # Thin, stateless wrappers that group ConPilot methods by concern.
        # They forward to the existing methods so Commit A introduces the
        # new ergonomic call-site syntax with zero behaviour change.
        self.agents = _AgentsAPI(self)
        self.cron = _CronAPI(self)
        self.projects = _ProjectsAPI(self)

    @property
    def config_store(self):
        """
        Return the :class:`ConfigStore` used to manage configuration versions.

        :return: the configuration store bound to this pilot.
        :rtype: `ConfigStore`
        """
        return self._config_store

    @property
    def snapshot_service(self):
        """
        Return the :class:`SnapshotService` used to manage ``.github`` snapshots.

        :return: the snapshot service bound to this pilot.
        :rtype: `SnapshotService`
        """
        return self._snapshot_service

    @property
    def github_token(self) -> GitHubToken | None:
        """
        Return the resolved GitHub token used by the Copilot SDK.

        Note:
            Resolved from ``COPILOT_GITHUB_TOKEN``, ``GH_TOKEN`` or
            ``GITHUB_TOKEN`` (in that order) at construction time.

        :return: the GitHub token, or ``None`` when no environment variable is set.
        :rtype: `GitHubToken | None`
        """
        return self._token

    @property
    def home(self) -> str:
        """
        Return the ``CONDUCTOR_HOME`` directory.

        :return: absolute path to the conductor home directory.
        :rtype: `str`
        """
        return self._paths.home

    # ── Paths (delegated to PathResolver) ──────────────────────────────────────

    @property
    def config_path(self) -> str:
        """
        Return the path to the active ``conductor.yaml`` (or ``.json``) file.

        :return: absolute path to the conductor configuration file.
        :rtype: `str`
        """
        return self._paths.config_path

    @property
    def agents_dir(self) -> str:
        """
        Return the project-scoped ``.github/agents/`` directory.

        :return: absolute path to the agents directory.
        :rtype: `str`
        """
        return self._paths.agents_dir

    @property
    def retired_dir(self) -> str:
        """
        Return the project-scoped ``.github/agents/retired/`` directory.

        :return: absolute path to the retired agents directory.
        :rtype: `str`
        """
        return self._paths.retired_dir

    @property
    def system_agents_dir(self) -> str:
        """
        Return the system-level agents directory.

        :return: absolute path to the system agents directory.
        :rtype: `str`
        """
        return self._paths.system_agents_dir

    @property
    def system_retired_dir(self) -> str:
        """
        Return the system-level retired agents directory.

        :return: absolute path to the system retired agents directory.
        :rtype: `str`
        """
        return self._paths.system_retired_dir

    @property
    def system_logs_dir(self) -> str:
        """
        Return the directory in which system-level agent logs are written.

        :return: absolute path to the system logs directory.
        :rtype: `str`
        """
        return self._paths.system_logs_dir

    @property
    def cron_dir(self) -> str:
        """
        Return the cron working directory (``pending.log``, ``processed.log``).

        :return: absolute path to the cron directory.
        :rtype: `str`
        """
        return self._paths.cron_dir

    @property
    def cron_state_dir(self) -> str:
        """
        Return the persistent cron state directory.

        :return: absolute path to the cron ``.state/`` directory.
        :rtype: `str`
        """
        return self._paths.cron_state_dir

    @property
    def pending_log(self) -> str:
        """
        Return the path to ``pending.log`` consumed by :class:`PendingDispatcher`.

        :return: absolute path to the pending tasks log.
        :rtype: `str`
        """
        return self._paths.pending_log

    @property
    def templates_dir(self) -> str:
        """
        Return the directory containing role and project templates.

        :return: absolute path to the templates directory.
        :rtype: `str`
        """
        return self._paths.templates_dir

    @property
    def trust_file(self) -> str:
        """
        Return the path to ``.github/trust.json``.

        :return: absolute path to the trust registry file.
        :rtype: `str`
        """
        return self._paths.trust_file

    @property
    def key_file(self) -> str:
        """
        Return the path to the persisted system admin key file.

        :return: absolute path to the admin key file.
        :rtype: `str`
        """
        return self._paths.key_file

    _DEV_NULL_KEY = "00000000-0000-0000-0000-000000000000"

    def _is_dev_build(self) -> bool:
        """Return True if this con-pilot install is a dev build.

        Detected solely by ``CONDUCTOR_ENV=DEV`` in the process environment.
        Any other value (or unset) means a normal/production build.
        """
        return os.environ.get("CONDUCTOR_ENV") == "DEV"

    def _load_or_generate_key(self) -> str:
        """Return the system key, generating and persisting a new GUID on first call.

        Dev builds always return the null GUID and never persist a key file.
        """
        import uuid

        if self._is_dev_build():
            return self._DEV_NULL_KEY

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
        """
        Return the on-disk root directory of a registered project.

        :param project: registered project name.
        :type project: `str`
        :return: absolute path to the project root.
        :rtype: `str`
        """
        return self._paths.project_dir(project)

    def project_agents_dir(self, project: str) -> str:
        """
        Return the ``.github/agents/`` directory for a project.

        :param project: registered project name.
        :type project: `str`
        :return: absolute path to the project agents directory.
        :rtype: `str`
        """
        return self._paths.project_agents_dir(project)

    def project_retired_dir(self, project: str) -> str:
        """
        Return the project retired-agents directory.

        :param project: registered project name.
        :type project: `str`
        :return: absolute path to the project retired agents directory.
        :rtype: `str`
        """
        return self._paths.project_retired_dir(project)

    def project_cron_dir(self, project: str) -> str:
        """
        Return the project-scoped cron directory.

        :param project: registered project name.
        :type project: `str`
        :return: absolute path to the project cron directory.
        :rtype: `str`
        """
        return self._paths.project_cron_dir(project)

    def _role_cron_root(self, role: str, project: str | None = None) -> str:
        """Return the cron directory for a role: project-scoped or system-level."""
        scope = self.config.get_agent_dict(role).get("scope", "system")
        if scope == "project" and project:
            return self.project_cron_dir(project)
        return self.cron_dir

    @property
    def sync_log(self) -> str:
        """
        Return the path of the sync log file.

        :return: absolute path to the sync log file.
        :rtype: `str`
        """
        return self._paths.sync_log

    @property
    def scheduler_db_path(self) -> str:
        """
        Return the SQLite file used by APScheduler for persistent jobs.

        :return: absolute path to ``cron-jobs.sqlite3`` under ``CONDUCTOR_HOME``.
        :rtype: `str`
        """
        return os.path.join(self.home, self.SCHEDULER_DB_FILENAME)

    @property
    def scheduler(self) -> AsyncIOScheduler:
        """
        Return the lazily-built APScheduler instance backed by SQLite.

        Note:
            The first access constructs the scheduler under a lock; subsequent
            calls return the cached instance.

        :return: an :class:`AsyncIOScheduler` whose jobstore is persisted under
            :attr:`scheduler_db_path`.
        :rtype: `AsyncIOScheduler`
        """
        with self._scheduler_lock:
            if self._scheduler is None:
                self._scheduler = AsyncIOScheduler(
                    jobstores={
                        "default": SQLAlchemyJobStore(
                            url=f"sqlite:///{self.scheduler_db_path}"
                        )
                    },
                    timezone=UTC,
                )
        return self._scheduler

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
        """
        Return the parsed conductor configuration.

        Note:
            The config is lazy-loaded on first access and cached for the
            lifetime of this instance. Use :meth:`reload_config` to refresh.

        :return: parsed and validated conductor configuration.
        :rtype: `ConductorConfig`
        """
        if self._cfg is None:
            data = self._load_config_file(self.config_path)
            self._cfg = Conductor.instance(data)
        return self._cfg

    def reload_config(self) -> ConductorConfig:
        """
        Discard the cached configuration and reload it from disk.

        Example:
            cfg = pilot.reload_config()

        :return: the freshly-loaded conductor configuration.
        :rtype: `ConductorConfig`
        """
        data = self._load_config_file(self.config_path)
        self._cfg = Conductor.instance(data)
        return self._cfg

    @property
    def schema_path(self) -> str:
        """
        Return the path to the bundled JSON schema for ``conductor.yaml``.

        :return: absolute path to ``conductor.schema.json``.
        :rtype: `str`
        """
        return self._paths.schema_path

    def validate(self, config_path: str | None = None) -> ValidationResult:
        """
        Validate a conductor configuration file against the JSON schema.

        Example:
            result = pilot.validate()
            if not result.valid:
                for err in result.errors:
                    print(err.message)

        Note:
            Both YAML (``.yaml``/``.yml``) and JSON (``.json``) files are
            accepted. When ``config_path`` is omitted, ``self.config_path`` is
            validated.

        :param config_path: optional path to the configuration file to
            validate; defaults to :attr:`config_path`.
        :type config_path: `str | None`
        :return: a :class:`ValidationResult` with ``valid=True`` and an empty
            ``errors`` list on success, or ``valid=False`` with the collected
            schema violations on failure.
        :rtype: `ValidationResult`
        """
        from jsonschema import Draft202012Validator

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
        """
        Return the default Copilot model declared in ``conductor.yaml``.

        :return: the value of ``models.default_model``.
        :rtype: `str`
        """
        return self.config.models.default_model

    @property
    def active_roles(self) -> set[str]:
        """
        Return the set of active role keys excluding the ``conductor`` role.

        :return: role names with ``active=True`` other than ``conductor``.
        :rtype: `set[str]`
        """
        return {
            k for k, v in self.config.agents.items() if k != "conductor" and v.active
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
        """
        Return the session environment variables derived from configuration.

        Example:
            for k, v in pilot.env.items():
                os.environ[k] = v

        Note:
            Combines values from ``conductor.yaml`` and ``trust.json`` into a
            single mapping suitable for exporting into a Copilot subprocess
            environment.

        :return: a mapping of environment variable names to string values.
        :rtype: `dict[str, str]`
        """
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
            raw = (
                role_cfg.name
                if role_cfg and role_cfg.name
                else (conductor_name or "conductor")
            )
            sidekick_name = self._expand_name(raw, project=project, rank=1)
        elif len(sidekick_roles) == 1:
            raw = active[sidekick_roles[0]].name or sidekick_roles[0]
            sidekick_name = self._expand_name(raw, project=project, rank=1)
        else:
            sidekick_name = conductor_name or "conductor"

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
            except Exception:  # noqa: BLE001, S110
                pass
        # package.json
        pkg = d / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
                if name := data.get("name"):
                    return name
            except Exception:  # noqa: BLE001, S110
                pass
        # .git/config remote URL
        git_config = d / ".git" / "config"
        if git_config.exists():
            try:
                for line in git_config.read_text().splitlines():
                    if "url = " in line:
                        url = line.split("url = ")[-1].strip()
                        name = url.rstrip("/").split("/")[-1]
                        name = name.removesuffix(".git")
                        if name:
                            return name
            except Exception:  # noqa: BLE001, S110
                pass
        # Fallback: directory basename
        return d.name or None

    def resolve_project(self, cwd: str | None = None) -> tuple[str, str] | None:
        """
        Resolve the ``(project_name, project_directory)`` pair for a directory.

        Example:
            resolved = pilot.resolve_project("/work/myrepo")
            if resolved:
                project, root = resolved

        Note:
            The strategy is: infer from ``pyproject.toml`` / ``package.json`` /
            ``.git`` remote URL / directory name; if that fails and ``stdin``
            is a TTY, prompt the user interactively. On success ``PROJECT_NAME``
            is exported into the environment and the project is registered in
            ``trust.json``.

        :param cwd: directory to resolve from; defaults to the current working
            directory.
        :type cwd: `str | None`
        :return: a ``(name, directory)`` tuple, or ``None`` when the project
            cannot be determined.
        :rtype: `tuple[str, str] | None`
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
          [scope]         → scope name (or removed if no project)
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
        log.debug(
            "Expanded name template '%s' with project='%s' and rank='%s' to '%s'",
            template,
            project,
            rank,
            name,
        )
        return name

    def start_watcher(self) -> int:
        """
        Spawn ``con-pilot serve`` as a detached background process.

        Example:
            pid = pilot.start_watcher()
            print(f"watcher pid={pid}")

        Note:
            Standard output and standard error are redirected to
            :attr:`sync_log` and the new process is detached via
            ``start_new_session=True``.

        :return: the PID of the spawned ``con-pilot`` process.
        :rtype: `int`
        """
        log_path = self.sync_log
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
        Register a new project and synchronise its agent files.

        Example:
            pilot.register("my-app", "/work/my-app")

        Note:
            Updates ``trust.json``, creates ``agents/``, ``retired/`` and
            ``cron/`` directories under the project root, exports
            ``PROJECT_NAME`` and runs :meth:`sync` to materialise project
            agents.

        :param name: human-readable project name (e.g. ``"my-app"``).
        :type name: `str`
        :param directory: absolute path to the project's root directory.
        :type directory: `str`
        :return: None
        :rtype: `None`
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
        Retire a project: move its directory aside and drop it from trust.

        Example:
            pilot.retire_project("old-app")

        Note:
            The project directory is moved from
            ``.github/projects/{name}`` to ``.github/retired-projects/{name}``
            (with a UTC timestamp suffix on collision) and the entry is
            removed from ``trust.json``.

        :param name: project name to retire.
        :type name: `str`
        :return: None
        :rtype: `None`
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
                from datetime import datetime

                stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")
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


    def print_env(self, shell: bool = False) -> None:
        """
        Print session environment variables to standard output.

        Example:
            pilot.print_env(shell=True)
            # export CONDUCTOR_HOME="/home/me/.conductor"
            # ...

        Note:
            Resolves the project (setting ``PROJECT_NAME``) and starts the
            background watcher (``SYNC_AGENTS_PID``) before emitting variables.
            When ``shell=True`` each line is formatted as ``export KEY="VAL"``
            so the output can be sourced by a POSIX shell.

        :param shell: when ``True`` emit ``export``-style lines instead of
            ``KEY=VAL`` pairs.
        :type shell: `bool`
        :return: None
        :rtype: `None`
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


    def sync(self, cwd: str | None = None) -> None:
        """
        Reconcile ``.agent.md`` files with ``conductor.yaml`` and dispatch cron jobs.

        Example:
            pilot.sync(cwd="/work/my-app")

        Note:
            System agents (``scope=system``) are written to
            ``.github/system/agents/``; project agents (``scope=project``) are
            written to ``.github/projects/{project}/agents/`` with numbered
            sub-files when ``instances.max`` is set. The project is resolved
            from ``cwd``, then ``PROJECT_NAME``, then filesystem inference,
            then an interactive prompt.

        :param cwd: optional working directory used as the starting point for
            project resolution; defaults to the current working directory.
        :type cwd: `str | None`
        :return: None
        :rtype: `None`
        """
        cfg = self.reload_config()
        roles = self.active_roles
        agent_cfg = cfg.agent_dicts

        os.makedirs(self.system_agents_dir, exist_ok=True)
        os.makedirs(self.system_retired_dir, exist_ok=True)

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

        # ── System agents in .github/system/agents/ ────────────────────────────
        expected_system: set[str] = {"conductor.agent.md"}
        for role in system_roles:
            expected_system.add(f"{role}.agent.md")

        for fname in os.listdir(self.system_agents_dir):
            if not fname.endswith(".agent.md") or fname == "conductor.agent.md":
                continue
            if fname not in expected_system:
                shutil.move(
                    os.path.join(self.system_agents_dir, fname),
                    os.path.join(self.system_retired_dir, fname),
                )
                log.info("Retired (system): %s", fname)

        for role in system_roles:
            fname = f"{role}.agent.md"
            dest = os.path.join(self.system_agents_dir, fname)
            if os.path.exists(dest):
                continue
            retired = os.path.join(self.system_retired_dir, fname)
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

        if self._scheduler is not None and self._scheduler.running:
            self._refresh_scheduled_task_jobs()

        self.cron(project=project_name)

    # ── HTTP service ────────────────────────────────────────────────────────────

    def create_app(self, interval: int | None = None):
        """
        Build and return the FastAPI application bound to this pilot.

        Example:
            app = pilot.create_app()
            uvicorn.run(app, host="127.0.0.1", port=8000)

        Note:
            Delegates to :func:`con_pilot.v1.api.create_app`, which wires the
            v1 routers and lifespan.

        :param interval: optional sync-loop interval in seconds; defaults to
            :attr:`DEFAULT_INTERVAL` when ``None``.
        :type interval: `int | None`
        :return: a configured :class:`fastapi.FastAPI` application instance.
        :rtype: `FastAPI`
        """
        from con_pilot.v1.api import create_app as _create_app

        return _create_app(self, interval=interval)

    def serve(self, interval: int | None = None) -> None:
        """
        Run the con-pilot FastAPI service under uvicorn until terminated.

        Example:
            pilot.serve(interval=300)

        Note:
            Host and port are read from ``$CONDUCTOR_HOME/.env`` under the
            ``[con-pilot]`` table, defaulting to ``localhost:8000``. Logs are
            routed through the project's loguru configuration.

        :param interval: optional sync-loop interval in seconds; defaults to
            :attr:`DEFAULT_INTERVAL` when ``None``.
        :type interval: `int | None`
        :return: None
        :rtype: `None`
        """
        import uvicorn

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
        log_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {
                    "()": "uvicorn.logging.DefaultFormatter",
                    "fmt": "[con-pilot %(asctime)s] %(levelname)s %(message)s",
                    "datefmt": "%H:%M:%S",
                    "use_colors": False,
                }
            },
            "handlers": {
                "file": {
                    "class": "logging.FileHandler",
                    "filename": self._paths.sync_log,
                    "formatter": "default",
                }
            },
            "loggers": {
                "uvicorn": {"handlers": ["file"], "level": "INFO", "propagate": False},
                "uvicorn.error": {
                    "handlers": ["file"],
                    "level": "INFO",
                    "propagate": False,
                },
                "uvicorn.access": {
                    "handlers": ["file"],
                    "level": "INFO",
                    "propagate": False,
                },
            },
        }
        uvicorn.run(app, host=host, port=port, log_config=log_config)


# ── Service facades ────────────────────────────────────────────────────────
#
# These classes group related ConPilot methods under a single attribute so
# call sites read as ``pilot.agents.list()`` rather than
# ``pilot.list_agents()``. They are thin and intentionally stateless:
# every method delegates straight to ConPilot. The implementations
# themselves live in ``con_pilot._facets`` and are mixed into ``ConPilot``
# via inheritance; the facade attribute names locked in here are the
# public API.


class _AgentsAPI:
    """Agent CRUD operations grouped under ``ConPilot.agents``."""

    def __init__(self, pilot: ConPilot) -> None:
        self._pilot = pilot

    def list(self, project: str | None = None) -> AgentListResponse:
        return self._pilot.list_agents(project=project)

    def list_configs(self) -> dict[str, AgentDetailResponse]:
        return self._pilot.list_agent_configs()

    def get(self, name: str) -> AgentDetailResponse | None:
        return self._pilot.get_agent(name)

    def get_config(self, name: str) -> AgentDetailResponse | None:
        return self._pilot.get_agent_config(name)

    def update_config(
        self, name: str, changes: dict[str, Any]
    ) -> AgentDetailResponse | None:
        return self._pilot.update_agent_config(name, changes)

    def amend(
        self,
        instructions_file: str,
        role: str,
        project: str | None = None,
        key: str | None = None,
    ) -> None:
        return self._pilot.amend_agent(instructions_file, role, project, key)

    def replace(
        self,
        instructions_file: str,
        role: str,
        project: str | None = None,
        key: str | None = None,
    ) -> None:
        return self._pilot.replace_agent(instructions_file, role, project, key)

    def reset(
        self,
        role: str,
        project: str | None = None,
        key: str | None = None,
    ) -> None:
        return self._pilot.reset_agent(role, project, key)


class _CronAPI:
    """Scheduling operations grouped under ``ConPilot.cron``.

    The instance is also callable: ``pilot.cron()`` runs the cron sweep
    (back-compat with the historical ``ConPilot.cron`` method).
    """

    def __init__(self, pilot: ConPilot) -> None:
        self._pilot = pilot

    def __call__(self, project: str | None = None) -> None:
        return self._pilot._cron_sweep(project=project)

    def list(self) -> list[dict[str, Any]]:
        return self._pilot.list_cron_jobs()

    def get(self, name: str) -> dict[str, Any] | None:
        return self._pilot.get_cron_job(name)

    def add(self, task_data: dict[str, Any]) -> dict[str, Any]:
        return self._pilot.add_cron_job(task_data)

    def update(
        self, name: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        return self._pilot.update_cron_job(name, changes)

    def remove(self, name: str) -> bool:
        return self._pilot.remove_cron_job(name)

    def read_logs(
        self, lines: int = 100, project: str | None = None
    ) -> dict[str, Any]:
        return self._pilot.read_cron_logs(lines=lines, project=project)

    def run_task(self, name: str) -> bool:
        return self._pilot.run_task(name)

    async def start_scheduler(self) -> None:
        await self._pilot.start_scheduler()

    async def stop_scheduler(self) -> None:
        await self._pilot.stop_scheduler()


class _ProjectsAPI:
    """Project lifecycle operations grouped under ``ConPilot.projects``."""

    def __init__(self, pilot: ConPilot) -> None:
        self._pilot = pilot

    def register(self, name: str, directory: str) -> None:
        return self._pilot.register(name, directory)

    def retire(self, name: str) -> None:
        return self._pilot.retire_project(name)

    def resolve(self, cwd: str | None = None) -> tuple[str, str] | None:
        return self._pilot.resolve_project(cwd=cwd)

    def start_watcher(self) -> int:
        return self._pilot.start_watcher()
