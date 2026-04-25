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
import textwrap
import tomllib
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any
import yaml
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

from con_pilot.auth import (
    GitHubToken,
    resolve_github_token,
)
from con_pilot.models import (
    Agent,
    AgentDetailResponse,
    AgentInfo,
    AgentListResponse,
    Conductor,
    ConductorConfig,
    ValidationError,
    ValidationResult,
)
from con_pilot.paths import PathResolver
from con_pilot.logger import app_logger
from con_pilot.trust import TrustRegistry

log = app_logger.bind(module=__name__)

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

    def list_agents(self, project: str | None = None) -> AgentListResponse:
        """
        List every agent defined in ``conductor.yaml`` with its on-disk status.

        Example:
            response = pilot.list_agents()
            for agent in response.system_agents + response.project_agents:
                print(agent.role, agent.scope, agent.file_exists)

        Note:
            Project-scoped agents are expanded across all registered projects
            from ``trust.json`` when ``project`` is omitted; multi-instance
            agents emit one entry per declared instance.

        :param project: optional project name used to filter project-scoped
            agents. When ``None``, every registered project is scanned.
        :type project: `str | None`
        :return: a response carrying ``system_agents`` and ``project_agents``
            lists with file existence, model and runtime metadata.
        :rtype: `AgentListResponse`
        """
        cfg = self.config
        system_agents: list[AgentInfo] = []
        project_agents: list[AgentInfo] = []

        # Get all registered projects from trust.json
        trust = self._load_trust()
        registered_projects = [p for p in trust if p != "conductor"]

        for role, agent_cfg in cfg.agents.items():
            scope = agent_cfg.scope
            instances = agent_cfg.instances
            max_inst = instances.max if instances else None

            if scope == "system":
                # System agent - single file in .github/system/agents/
                fname = f"{role}.agent.md"
                fpath = os.path.join(self.system_agents_dir, fname)
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
                        running=agent_cfg.running
                        if isinstance(agent_cfg, Agent)
                        else False,
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
                                    running=agent_cfg.running
                                    if isinstance(agent_cfg, Agent)
                                    else False,
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
                                running=agent_cfg.running
                                if isinstance(agent_cfg, Agent)
                                else False,
                            )
                        )

        return AgentListResponse(
            system_agents=system_agents,
            project_agents=project_agents,
        )

    def _resolve_agent_role(self, name: str) -> str | None:
        """Resolve an agent role by role key first, then display name."""
        cfg = self.config
        role = name if name in cfg.agents else None
        if role is None:
            for r, a in cfg.agents.items():
                if (a.name or r).lower() == name.lower():
                    role = r
                    break
        return role

    def list_agent_configs(self) -> dict[str, AgentDetailResponse]:
        """
        Return runtime descriptions for every configured agent.

        Example:
            for role, detail in pilot.list_agent_configs().items():
                print(role, detail.active)

        :return: a mapping of role keys to their :class:`AgentDetailResponse`.
        :rtype: `dict[str, AgentDetailResponse]`
        """
        result: dict[str, AgentDetailResponse] = {}
        for role in self.config.agents:
            detail = self.get_agent(role)
            if detail is not None:
                result[role] = detail
        return result

    def get_agent_config(self, name: str) -> AgentDetailResponse | None:
        """
        Return the runtime description of a single agent.

        Example:
            detail = pilot.get_agent_config("developer")

        :param name: the role key (e.g. ``"developer"``) or the display name
            of the agent.
        :type name: `str`
        :return: the resolved agent description, or ``None`` when no matching
            role is found.
        :rtype: `AgentDetailResponse | None`
        """
        role = self._resolve_agent_role(name)
        if role is None:
            return None
        return self.get_agent(role)

    def update_agent_config(
        self, name: str, changes: dict[str, Any]
    ) -> AgentDetailResponse | None:
        """
        Update mutable fields of one agent and persist the result to disk.

        Example:
            pilot.update_agent_config("developer", {"active": False})

        Note:
            Only the fields ``name``, ``active``, ``sidekick``, ``augmenting``,
            ``model``, ``description`` and ``instructions`` may be changed.
            The configuration file is rewritten in its original format (YAML
            or JSON) and the in-memory cache is invalidated.

        :param name: the agent role key or display name.
        :type name: `str`
        :param changes: a mapping of field name to new value.
        :type changes: `dict[str, Any]`
        :return: the refreshed agent description, or ``None`` when ``name``
            does not match a configured role.
        :rtype: `AgentDetailResponse | None`
        :raises ValueError: when ``changes`` is empty, contains an unsupported
            field, or attempts to set ``name`` to an empty string.
        """
        role = self._resolve_agent_role(name)
        if role is None:
            return None

        mutable_fields = {
            "name",
            "active",
            "sidekick",
            "augmenting",
            "model",
            "description",
            "instructions",
        }
        unknown = [k for k in changes if k not in mutable_fields]
        if unknown:
            raise ValueError(f"Unsupported field(s): {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("No changes provided")

        agent_cfg = self.config.agents[role]
        if "name" in changes:
            new_name = changes["name"]
            if new_name is not None and str(new_name).strip() == "":
                raise ValueError("Field 'name' cannot be empty")

        for field, value in changes.items():
            setattr(agent_cfg, field, value)

        config_payload = self.config.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        with open(self.config_path, "w") as f:
            if self.config_path.endswith((".yaml", ".yml")):
                yaml.safe_dump(
                    config_payload, f, default_flow_style=False, sort_keys=False
                )
            else:
                json.dump(config_payload, f, indent=2)

        self.reload_config()
        return self.get_agent_config(role)

    def get_agent(self, name: str) -> AgentDetailResponse | None:
        """
        Return detailed information for a single agent.

        Example:
            detail = pilot.get_agent("developer")
            if detail is not None and detail.file_exists:
                ...

        Note:
            File-existence checks are performed only for system-scoped agents.
            Project-scoped agents are reported with ``file_exists=False`` and
            ``file_path=None``.

        :param name: agent role key (e.g. ``"developer"``) or display name.
        :type name: `str`
        :return: the agent description, or ``None`` when ``name`` does not
            match any configured role.
        :rtype: `AgentDetailResponse | None`
        """
        cfg = self.config

        role = self._resolve_agent_role(name)

        if role is None:
            return None

        agent_cfg = cfg.agents[role]
        scope = agent_cfg.scope

        # Resolve file existence for system-scoped agents only.
        if scope == "system":
            fpath = os.path.join(self.system_agents_dir, f"{role}.agent.md")
            file_exists = os.path.exists(fpath)
            file_path = fpath if file_exists else None
        else:
            file_exists = False
            file_path = None

        tasks = cfg.get_tasks_for_agent(role)
        permissions = agent_cfg.get_permissions().to_list()

        return AgentDetailResponse(
            role=role,
            name=self._expand_name(agent_cfg.name or role),
            scope=scope,
            active=agent_cfg.active,
            file_exists=file_exists,
            file_path=file_path,
            sidekick=agent_cfg.sidekick,
            augmenting=agent_cfg.augmenting,
            model=agent_cfg.model,
            description=agent_cfg.description,
            running=agent_cfg.running if isinstance(agent_cfg, Agent) else False,
            permissions=permissions,
            tasks=tasks,
            cron=agent_cfg.cron,
            instances=agent_cfg.instances,
            instructions=agent_cfg.instructions,
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

        For system agents (or when project is None) looks in .github/system/agents/.
        For project agents looks in .github/projects/{project}/agents/.
        """
        scope = self.config.get_agent_dict(role).get("scope", "system")
        if scope == "project" and project:
            search_dir = self.project_agents_dir(project)
            # Match role.project.agent.md and role.project.N.agent.md
            prefix = f"{role}.{project}."
        else:
            search_dir = self.system_agents_dir
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
        Amend agent files for a role by merging an ``## Instructions`` section.

        Example:
            pilot.amend_agent("new-instructions.md", role="developer")

        Note:
            Any pre-existing ``## Instructions`` block is replaced; all other
            sections of the agent file (including the YAML frontmatter) are
            preserved. System-scoped agents require ``key`` to match the
            persisted system key; the ``conductor`` role can never be amended.

        :param instructions_file: path to a markdown file whose contents form
            the new ``## Instructions`` body.
        :type instructions_file: `str`
        :param role: agent role key (e.g. ``"developer"``).
        :type role: `str`
        :param project: optional project name; required for project-scoped
            agents.
        :type project: `str | None`
        :param key: system admin key required to mutate system-scoped agents.
        :type key: `str | None`
        :return: None
        :rtype: `None`
        :raises ValueError: when targeting the ``conductor`` role or when
            ``key`` does not match the system key for a system-scoped agent.
        :raises FileNotFoundError: when no agent file matches the role/project.
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
        Replace the body of agent files for a role while preserving frontmatter.

        Example:
            pilot.replace_agent("new-body.md", role="developer")

        Note:
            The YAML frontmatter is preserved; everything below it is replaced
            with the contents of ``instructions_file``. System-scoped agents
            require ``key`` to match the persisted system key.

        :param instructions_file: path to the file whose contents become the
            new agent body.
        :type instructions_file: `str`
        :param role: agent role key.
        :type role: `str`
        :param project: optional project name; required for project-scoped
            agents.
        :type project: `str | None`
        :param key: system admin key required for system-scoped agents.
        :type key: `str | None`
        :return: None
        :rtype: `None`
        :raises ValueError: when targeting the ``conductor`` role or when the
            system key does not match.
        :raises FileNotFoundError: when no matching agent file exists.
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
        Reset agent files for a role to their template-derived content.

        Example:
            pilot.reset_agent("developer", project="my-app")

        Note:
            Existing files are overwritten using the role template (or
            regenerated from ``conductor.yaml`` when no template is found).
            System-scoped agents require ``key`` to match the system key.

        :param role: agent role key.
        :type role: `str`
        :param project: optional project name; required for project-scoped
            agents.
        :type project: `str | None`
        :param key: system admin key required for system-scoped agents.
        :type key: `str | None`
        :return: None
        :rtype: `None`
        :raises ValueError: when targeting the ``conductor`` role or when the
            system key does not match.
        :raises FileNotFoundError: when no matching agent file exists.
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
        os.makedirs(self.system_agents_dir, exist_ok=True)
        os.makedirs(self.system_retired_dir, exist_ok=True)
        os.makedirs(self.system_logs_dir, exist_ok=True)

        agent_cfg = self.config.agent_dicts

        # Build list: conductor first, then other active system-scoped roles
        system_roles = ["conductor"] + [
            role
            for role, cfg in agent_cfg.items()
            if role != "conductor"
            and cfg.get("active", False)
            and cfg.get("scope", "system") != "project"
        ]
        app_logger.debug(
            "Ensuring system agents exist for roles", system_roles=system_roles
        )
        for role in system_roles:
            fname = f"{role}.agent.md"
            dest = os.path.join(self.system_agents_dir, fname)
            if os.path.exists(dest):
                continue
            retired = os.path.join(self.system_retired_dir, fname)
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

    # ── Cron ───────────────────────────────────────────────────────────────────

    def _scheduler_job_id(self, task_name: str) -> str:
        return f"task::{task_name}"

    def _cron_trigger(self, expression: str) -> CronTrigger:
        parts = expression.split()
        if len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
            return CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                timezone=UTC,
            )
        if len(parts) == 6:
            second, minute, hour, day, month, day_of_week = parts
            return CronTrigger(
                second=second,
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
                timezone=UTC,
            )
        raise ValueError(
            f"Cron expression must have 5 or 6 fields, got {len(parts)}: {expression!r}"
        )

    def _queue_task_from_scheduler(self, task_name: str) -> None:
        """Queue a scheduled top-level task into pending.log for the target agent."""
        cfg = self.reload_config()
        task = next((t for t in cfg.tasks if t.name == task_name), None)
        if task is None or task.cron is None:
            log.warning("Scheduled task not found or not cron-enabled: %s", task_name)
            return
        self._queue_task(task_name, source="scheduler")

    def _queue_task(self, task_name: str, *, source: str = "manual") -> bool:
        """Append a task into the appropriate pending.log. Returns True if queued."""
        cfg = self.reload_config()
        task = next((t for t in cfg.tasks if t.name == task_name), None)
        if task is None:
            log.warning("Task not found: %s", task_name)
            return False

        role = task.agent
        role_cfg = cfg.get_agent_dict(role)
        if not role_cfg:
            log.warning("Skipping task %s: unknown agent role %s", task.name, role)
            return False

        if not role_cfg.get("active", role == "conductor"):
            log.info("Skipping task %s: agent role %s is inactive", task.name, role)
            return False

        role_project: str | None = None
        if role_cfg.get("scope", "system") == "project":
            role_project = os.environ.get("PROJECT_NAME") or None
            if not role_project:
                log.warning(
                    "Skipping task %s: PROJECT_NAME is not set for project-scoped role %s",
                    task.name,
                    role,
                )
                return False

        agent_name = role_cfg.get("name", role)
        if role_project:
            agent_name = self._expand_name(agent_name, project=role_project)

        self._append_pending(
            role,
            agent_name,
            task.name,
            task.instructions,
            task.cron or "manual",
            project=role_project,
        )
        log.info(
            "Queued task (%s): [%s] %s — %s...",
            source,
            role,
            task.name,
            task.instructions[:60],
        )
        return True

    def run_task(self, task_name: str) -> bool:
        """
        Queue a configured task for execution by appending it to ``pending.log``.

        Example:
            queued = pilot.run_task("agent-health-check")

        Note:
            Works for both scheduled (cron) and manual tasks. The pending
            dispatcher (when running) will pick the entry up on its next tick;
            project-scoped tasks require ``PROJECT_NAME`` to be set.

        :param task_name: name of a task declared in ``conductor.yaml``.
        :type task_name: `str`
        :return: ``True`` when the task was queued, ``False`` when it does
            not exist, is inactive, or is missing required project context.
        :rtype: `bool`
        """
        return self._queue_task(task_name, source="manual")

    def _refresh_scheduled_task_jobs(self) -> None:
        """Mirror all config.tasks with cron expressions into APScheduler jobs."""
        cfg = self.reload_config()
        desired_job_ids: set[str] = set()

        for task in cfg.scheduled_tasks:
            job_id = self._scheduler_job_id(task.name)
            desired_job_ids.add(job_id)

            try:
                trigger = self._cron_trigger(task.cron or "")
            except ValueError as exc:
                log.warning(
                    "Skipping task %s due to invalid cron expression %r: %s",
                    task.name,
                    task.cron,
                    exc,
                )
                continue

            self.scheduler.add_job(
                _run_persisted_task_job,
                trigger=trigger,
                id=job_id,
                replace_existing=True,
                kwargs={"conductor_home": self.home, "task_name": task.name},
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )

        for job in self.scheduler.get_jobs():
            if job.id.startswith("task::") and job.id not in desired_job_ids:
                self.scheduler.remove_job(job.id)

    async def start_scheduler(self) -> None:
        """
        Start APScheduler on the running event loop and load configured tasks.

        Example:
            await pilot.start_scheduler()

        Note:
            Idempotent: returns early when the scheduler is already running.
            All tasks declared in ``conductor.yaml`` with a ``cron`` expression
            are mirrored into APScheduler jobs prefixed with ``task::``.

        :return: None
        :rtype: `None`
        """
        with self._scheduler_lock:
            if self.scheduler.running:
                return
            self._refresh_scheduled_task_jobs()
            self.scheduler.start()
            log.info(
                "APScheduler started (db=%s, jobs=%d)",
                self.scheduler_db_path,
                len(self.scheduler.get_jobs()),
            )

    async def stop_scheduler(self) -> None:
        """
        Stop APScheduler without waiting for in-flight jobs.

        Example:
            await pilot.stop_scheduler()

        Note:
            Idempotent: returns early when no scheduler is running.

        :return: None
        :rtype: `None`
        """
        with self._scheduler_lock:
            if self._scheduler is None or not self._scheduler.running:
                return
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
            log.info("APScheduler stopped")

    # ── Cron job management (API surface) ──────────────────────────────────────

    _CRON_TASK_MUTABLE_FIELDS: tuple[str, ...] = (
        "agent",
        "description",
        "instructions",
        "cron",
        "create_on_ping",
        "permissions",
    )

    def _describe_cron_job(self, task_name: str) -> dict[str, Any] | None:
        """Return a dict describing a configured cron task and its scheduler state."""
        from con_pilot.core.models.config import TaskConfig

        cfg = self.config
        task: TaskConfig | None = next(
            (t for t in cfg.tasks if t.name == task_name), None
        )
        if task is None:
            return None

        job_id = self._scheduler_job_id(task.name)
        next_run: str | None = None
        registered = False
        with self._scheduler_lock:
            scheduler = self._scheduler
            if scheduler is not None:
                job = scheduler.get_job(job_id)
                if job is not None:
                    registered = True
                    if job.next_run_time is not None:
                        next_run = job.next_run_time.isoformat()

        return {
            "name": task.name,
            "agent": task.agent,
            "description": task.description,
            "instructions": task.instructions,
            "cron": task.cron,
            "create_on_ping": task.create_on_ping,
            "permissions": list(task.permissions) if task.permissions else None,
            "scheduled": task.cron is not None,
            "registered": registered,
            "next_run_time": next_run,
            "job_id": job_id if registered else None,
        }

    def list_cron_jobs(self) -> list[dict[str, Any]]:
        """
        List every configured task with its scheduler registration state.

        Example:
            for job in pilot.list_cron_jobs():
                print(job["name"], job["scheduled"], job["registered"])

        :return: a list of dicts with keys ``name``, ``agent``, ``description``,
            ``instructions``, ``cron``, ``create_on_ping``, ``permissions``,
            ``scheduled``, ``registered``, ``next_run_time`` and ``job_id``.
        :rtype: `list[dict[str, Any]]`
        """
        return [self._describe_cron_job(task.name) or {} for task in self.config.tasks]

    def get_cron_job(self, name: str) -> dict[str, Any] | None:
        """
        Return the description of a single configured task.

        :param name: task name as declared in ``conductor.yaml``.
        :type name: `str`
        :return: the same shape as one entry of :meth:`list_cron_jobs`, or
            ``None`` when no task with that name exists.
        :rtype: `dict[str, Any] | None`
        """
        return self._describe_cron_job(name)

    def read_cron_logs(
        self, *, lines: int | None = None, project: str | None = None
    ) -> dict[str, Any]:
        """
        Return tail lines from a cron ``pending.log`` file.

        Example:
            tail = pilot.read_cron_logs(lines=100)

        Note:
            When ``project`` is provided the project-scoped log under
            ``.github/projects/{project}/cron/pending.log`` is used; otherwise
            the system-level ``pending.log`` is read.

        :param lines: maximum number of trailing lines to return; ``None``
            returns the full file.
        :type lines: `int | None`
        :param project: optional project name selecting a project-scoped log.
        :type project: `str | None`
        :return: a mapping with keys ``path``, ``total`` (line count) and
            ``lines`` (selected lines without trailing newlines).
        :rtype: `dict[str, Any]`
        """
        if project:
            log_path = os.path.join(self.project_cron_dir(project), "pending.log")
        else:
            log_path = self.pending_log

        if not os.path.exists(log_path):
            return {"path": log_path, "lines": [], "total": 0}

        with open(log_path) as f:
            all_lines = f.readlines()

        total = len(all_lines)
        selected = all_lines if lines is None else all_lines[-lines:]
        return {
            "path": log_path,
            "total": total,
            "lines": [line.rstrip("\n") for line in selected],
        }

    def _persist_config(self) -> None:
        """Write the current in-memory config back to ``self.config_path``."""
        payload = self.config.model_dump(mode="json", by_alias=True, exclude_none=True)
        with open(self.config_path, "w") as f:
            if self.config_path.endswith((".yaml", ".yml")):
                yaml.safe_dump(payload, f, default_flow_style=False, sort_keys=False)
            else:
                json.dump(payload, f, indent=2)

    def _reschedule_after_config_change(self) -> None:
        """Mirror the refreshed config into APScheduler if it is running."""
        with self._scheduler_lock:
            scheduler = self._scheduler
            if scheduler is None or not scheduler.running:
                return
        self._refresh_scheduled_task_jobs()

    def add_cron_job(self, task_data: dict[str, Any]) -> dict[str, Any]:
        """
        Append a new task to ``conductor.yaml`` and refresh the scheduler.

        Example:
            pilot.add_cron_job({
                "name": "nightly", "agent": "developer",
                "description": "...", "instructions": "...",
                "cron": "0 2 * * *",
            })

        :param task_data: dict matching the :class:`TaskConfig` schema; the
            keys ``name``, ``agent``, ``description`` and ``instructions``
            are required.
        :type task_data: `dict[str, Any]`
        :return: the description of the newly-added job (same shape as
            :meth:`get_cron_job`).
        :rtype: `dict[str, Any]`
        :raises ValueError: when required fields are missing, the payload
            fails validation, the task name already exists, or ``agent`` is
            not a configured role.
        """
        from con_pilot.core.models.config import TaskConfig

        required = ("name", "agent", "description", "instructions")
        missing = [f for f in required if not task_data.get(f)]
        if missing:
            raise ValueError(f"Missing required field(s): {', '.join(missing)}")

        try:
            new_task = TaskConfig(**task_data)
        except Exception as exc:
            raise ValueError(f"Invalid task definition: {exc}") from exc

        cfg = self.config
        if any(t.name == new_task.name for t in cfg.tasks):
            raise ValueError(f"Task '{new_task.name}' already exists")
        if new_task.agent not in cfg.agents:
            raise ValueError(f"Unknown agent role: {new_task.agent}")

        cfg.tasks.append(new_task)
        self._persist_config()
        self.reload_config()
        self._reschedule_after_config_change()

        result = self._describe_cron_job(new_task.name)
        assert result is not None
        return result

    def update_cron_job(
        self, name: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """
        Update mutable fields of an existing task and refresh the scheduler.

        Example:
            pilot.update_cron_job("nightly", {"cron": "0 3 * * *"})

        Note:
            Allowed fields are ``agent``, ``description``, ``instructions``,
            ``cron``, ``create_on_ping`` and ``permissions``. The configuration
            is rewritten and the scheduler is refreshed when running.

        :param name: task name to update.
        :type name: `str`
        :param changes: mapping of field name to new value.
        :type changes: `dict[str, Any]`
        :return: the refreshed job description, or ``None`` when ``name``
            does not match a configured task.
        :rtype: `dict[str, Any] | None`
        :raises ValueError: when ``changes`` is empty, contains an unsupported
            field, fails validation, or references an unknown agent role.
        """
        from con_pilot.core.models.config import TaskConfig

        unknown = [k for k in changes if k not in self._CRON_TASK_MUTABLE_FIELDS]
        if unknown:
            raise ValueError(f"Unsupported field(s): {', '.join(sorted(unknown))}")
        if not changes:
            raise ValueError("No changes provided")

        cfg = self.config
        idx = next((i for i, t in enumerate(cfg.tasks) if t.name == name), None)
        if idx is None:
            return None

        existing = cfg.tasks[idx]
        merged = existing.model_dump()
        merged.update(changes)
        try:
            updated = TaskConfig(**merged)
        except Exception as exc:
            raise ValueError(f"Invalid task definition: {exc}") from exc

        if updated.agent not in cfg.agents:
            raise ValueError(f"Unknown agent role: {updated.agent}")

        cfg.tasks[idx] = updated
        self._persist_config()
        self.reload_config()
        self._reschedule_after_config_change()
        return self._describe_cron_job(name)

    def remove_cron_job(self, name: str) -> bool:
        """
        Remove a task from configuration and unregister its scheduler job.

        Example:
            removed = pilot.remove_cron_job("nightly")

        :param name: name of the task to remove.
        :type name: `str`
        :return: ``True`` when the task existed and was removed, ``False``
            when no task with that name was found.
        :rtype: `bool`
        """
        cfg = self.config
        before = len(cfg.tasks)
        cfg.tasks = [t for t in cfg.tasks if t.name != name]
        if len(cfg.tasks) == before:
            return False

        self._persist_config()
        self.reload_config()

        with self._scheduler_lock:
            scheduler = self._scheduler
            if scheduler is not None and scheduler.running:
                try:
                    scheduler.remove_job(self._scheduler_job_id(name))
                except Exception as exc:  # noqa: BLE001
                    log.debug("remove_job(%s) failed: %s", name, exc)
        self._reschedule_after_config_change()
        return True

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
        # Wake the dispatcher (no-op if not running).
        dispatcher = getattr(self, "_dispatcher", None)
        if dispatcher is not None:
            dispatcher.notify()

    def cron(self, project: str | None = None) -> None:
        """
        Walk every active role and queue any cron jobs that are due.

        Example:
            pilot.cron()

        Note:
            Reads each role's ``{role}.cron`` TOML file (creating a placeholder
            on first run), checks every declared ``[[job]]`` against its
            ``last_run`` state file and appends due entries to ``pending.log``.

        :param project: when provided, restricts the scan to project-scoped
            roles attached to this project.
        :type project: `str | None`
        :return: None
        :rtype: `None`
        """
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
            except Exception as exc:  # noqa: BLE001
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


def _run_persisted_task_job(conductor_home: str, task_name: str) -> None:
    """APScheduler job entrypoint that can be serialized in SQLAlchemyJobStore."""
    pilot = ConPilot(conductor_home=conductor_home, require_token=False)
    pilot._queue_task_from_scheduler(task_name)
