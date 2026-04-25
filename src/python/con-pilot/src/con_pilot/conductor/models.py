"""Configuration models for conductor.json parsing and validation."""

from __future__ import annotations


import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, ClassVar, Literal, Self

from croniter import croniter
from pydantic import BaseModel, Field, field_validator, model_validator

from con_pilot.exceptions import AgentNamingPatternException


class Permission(StrEnum):
    """Enumeration of all agent permissions.

    Can be used in YAML as a list of strings:
        permissions:
          - workspace_read
          - file_create
          - git_commit

    Organized by category:
    - workspace: Context and workspace access
    - file: File system operations
    - terminal: Command execution with safety controls
    - git: Version control operations including PR creation
    - agent: Agent invocation and management
    - config: Configuration access
    - project: Project lifecycle
    - cron: Scheduled job management
    - autonomy: Autonomous operation controls
    - admin: Administrative operations
    """

    # Workspace & Context Access
    WORKSPACE_READ = "workspace_read"
    CODEBASE_CONTEXT = "codebase_context"

    # File operations
    FILE_CREATE = "file_create"
    FILE_MODIFY = "file_modify"
    FILE_DELETE = "file_delete"
    FILE_MODIFY_OUTSIDE_WORKSPACE = "file_modify_outside_workspace"

    # Terminal operations
    TERMINAL_EXECUTE = "terminal_execute"
    TERMINAL_BACKGROUND = "terminal_background"
    TERMINAL_AUTO_APPROVE = "terminal_auto_approve"
    TERMINAL_DESTRUCTIVE = "terminal_destructive"

    # Git operations
    GIT_READ = "git_read"
    GIT_COMMIT = "git_commit"
    GIT_BRANCH = "git_branch"
    GIT_PUSH = "git_push"
    GIT_MERGE = "git_merge"
    GIT_PR_CREATE = "git_pr_create"
    GIT_PR_REVIEW = "git_pr_review"

    # Agent operations
    AGENT_INVOKE = "agent_invoke"
    AGENT_MANAGE = "agent_manage"

    # Config operations
    CONFIG_READ = "config_read"
    CONFIG_WRITE = "config_write"

    # Project operations
    PROJECT_REGISTER = "project_register"
    PROJECT_RETIRE = "project_retire"

    # Cron operations
    CRON_READ = "cron_read"
    CRON_MANAGE = "cron_manage"

    # Autonomy controls
    AUTONOMOUS_MODE = "autonomous_mode"
    REQUIRE_APPROVAL_DESTRUCTIVE = "require_approval_destructive"
    TRUSTED_DIRECTORIES_ONLY = "trusted_directories_only"

    # Admin operations
    ADMIN_FULL = "admin_full"

    @classmethod
    def all_values(cls) -> list[str]:
        """
        Return every permission as its raw string value.

        :return: list of permission identifiers in declaration order.
        :rtype: `list[str]`
        """
        return [p.value for p in cls]

    @classmethod
    def from_string(cls, value: str) -> "Permission":
        """
        Resolve a string to a :class:`Permission` member.

        :param value: permission identifier (e.g. ``"workspace_read"``).
        :type value: `str`
        :return: matching :class:`Permission` member.
        :rtype: `Permission`
        :raises ValueError: when no member matches ``value``.
        """
        try:
            return cls(value)
        except ValueError:
            raise ValueError(f"Unknown permission: {value}") from None


class VersionConfig(BaseModel):
    """Version information for the conductor configuration."""

    number: str = Field(
        ...,
        description="Semantic version number (e.g., '1.0.0').",
        pattern=r"^\d+\.\d+\.\d+$",
    )
    description: str | None = Field(
        default=None,
        description="Brief description of this configuration version.",
    )
    date: datetime = Field(
        ...,
        description="ISO 8601 datetime when this version was created or updated.",
    )
    notes: str | None = Field(
        default=None,
        description="Additional notes or changelog for this version.",
    )


class CronConfig(BaseModel):
    """Cron job configuration for an agent.

    Uses croniter-compatible cron expressions (5 or 6 fields).
    Examples:
        - "0 9 * * *" - Daily at 9am
        - "*/15 * * * *" - Every 15 minutes
        - "0 0 * * 0" - Weekly on Sunday at midnight
    """

    expression: str = Field(
        ...,
        description="Cron expression (5 or 6 fields). Validated using croniter.",
        examples=["0 9 * * *", "*/15 * * * *", "0 0 * * 0"],
    )
    file: str | None = Field(
        default=None,
        description=(
            "Custom cron file path relative to .github/agents/cron/."
            " Defaults to <role>.cron if absent."
        ),
    )

    @field_validator("expression")
    @classmethod
    def validate_cron_expression(cls, v: str) -> str:
        """
        Validate a cron expression with ``croniter``.

        :param v: cron expression to validate.
        :type v: `str`
        :return: the validated expression unchanged.
        :rtype: `str`
        :raises ValueError: when ``v`` is not a valid cron expression.
        """
        if not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v}")
        return v


class TaskConfig(BaseModel):
    """Configuration for a scheduled or triggered task.

    Tasks are top-level definitions that reference an agent to execute them.
    Only tasks with a cron expression are scheduled; others are triggered manually.
    """

    name: str = Field(
        ...,
        description="Unique identifier for this task.",
    )
    agent: str = Field(
        ...,
        description="The agent role key that should execute this task (e.g., 'git', 'reviewer').",
    )
    description: str = Field(
        ...,
        description="Human-readable description of what this task does.",
    )
    instructions: str = Field(
        ...,
        description="Detailed instructions for the agent when executing this task.",
    )
    cron: str | None = Field(
        default=None,
        description="Cron expression for scheduling. If None, task is only triggered manually.",
    )
    create_on_ping: bool = Field(
        default=False,
        description=(
            "If True, create the agent when cron triggers if it doesn't exist."
            " Otherwise, just log that the agent is missing."
        ),
    )
    permissions: list[str] | None = Field(
        default=None,
        description=(
            "List of permissions required for this task."
            " If the agent lacks these, the task is skipped with a warning."
        ),
    )

    @field_validator("cron")
    @classmethod
    def validate_cron_expression(cls, v: str | None) -> str | None:
        """
        Validate an optional cron expression with ``croniter``.

        :param v: cron expression, or ``None`` for manually triggered tasks.
        :type v: `str | None`
        :return: the validated value (``None`` is returned unchanged).
        :rtype: `str | None`
        :raises ValueError: when ``v`` is provided but invalid.
        """
        if v is not None and not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v}")
        return v


class InstancePolicy(BaseModel):
    """Concurrency policy: minimum and optional maximum number of agent instances.

    - min: The minimum number of instances that must always be created/running.
    - max: The upper cap on instances (defaults to min if not set).

    Use `creation_range()` when creating/ensuring agent files exist.
    Use `capacity_range()` when listing all possible slots.
    """

    min: int = Field(
        default=1, ge=1, description="Minimum number of instances that must be running."
    )
    max: int | None = Field(
        default=None,
        ge=1,
        description="Maximum concurrent instances allowed. Defaults to min if not set.",
    )

    @property
    def effective_max(self) -> int:
        """
        Return the upper instance bound, falling back to :attr:`min`.

        :return: ``max`` when set, otherwise ``min``.
        :rtype: `int`
        """
        return self.max if self.max is not None else self.min

    def creation_range(self) -> range:
        """
        Return the inclusive instance numbers that must be created.

        :return: ``range(1, min + 1)``.
        :rtype: `range`
        """
        return range(1, self.min + 1)

    def capacity_range(self) -> range:
        """
        Return the inclusive instance numbers permitted by the policy.

        :return: ``range(1, effective_max + 1)``.
        :rtype: `range`
        """
        return range(1, self.effective_max + 1)

    @property
    def is_multi_instance(self) -> bool:
        """
        Indicate whether the policy permits more than one instance.

        :return: ``True`` when ``effective_max`` is greater than one.
        :rtype: `bool`
        """
        return self.effective_max > 1

    def __iter__(self):
        """Iterate over instance numbers that must be created (1 to min)."""
        yield from self.creation_range()


class AgentPermissions(BaseModel):
    """Granular permissions for an agent, modeled after GitHub Copilot Agent permissions.

    Permissions are organized by category:
    - workspace: Context and workspace access
    - file: File system operations
    - terminal: Command execution with safety controls
    - git: Version control operations including PR creation
    - agent: Agent invocation and management
    - config: Configuration access
    - project: Project lifecycle
    - cron: Scheduled job management
    - autonomy: Autonomous operation controls
    - admin: Administrative operations

    The conductor agent always has all permissions and cannot be modified.
    Safety controls (require_approval_*, trusted_directories_only) can restrict
    even when base permissions are granted.
    """

    # Workspace & Context Access
    workspace_read: bool = Field(
        default=True,
        description="Permission to read files and code structure in workspace.",
    )
    codebase_context: bool = Field(
        default=True,
        description="Access to codebase knowledge (#codebase, @workspace in chat).",
    )

    # File operations
    file_create: bool = Field(
        default=False,
        description="Permission to create new files.",
    )
    file_modify: bool = Field(
        default=False,
        description="Permission to modify existing files.",
    )
    file_delete: bool = Field(
        default=False,
        description="Permission to delete files (destructive action).",
    )
    file_modify_outside_workspace: bool = Field(
        default=False,
        description="Permission to modify files outside current workspace (destructive).",
    )

    # Terminal operations
    terminal_execute: bool = Field(
        default=False,
        description="Permission to execute terminal commands (touch, sed, node, etc.).",
    )
    terminal_background: bool = Field(
        default=False,
        description="Permission to run background/async processes.",
    )
    terminal_auto_approve: bool = Field(
        default=False,
        description="Auto-approve terminal commands without prompts.",
    )
    terminal_destructive: bool = Field(
        default=False,
        description="Allow destructive terminal commands (rm, chmod, etc.).",
    )

    # Git operations
    git_read: bool = Field(
        default=True,
        description="Permission to read git status, log, diff.",
    )
    git_commit: bool = Field(
        default=False,
        description="Permission to stage and commit changes.",
    )
    git_branch: bool = Field(
        default=False,
        description="Permission to create, delete, or switch branches.",
    )
    git_push: bool = Field(
        default=False,
        description="Permission to push to remote repositories.",
    )
    git_merge: bool = Field(
        default=False,
        description="Permission to merge branches.",
    )
    git_pr_create: bool = Field(
        default=False,
        description="Permission to create pull requests.",
    )
    git_pr_review: bool = Field(
        default=False,
        description="Permission to review and comment on pull requests.",
    )

    # Agent operations
    agent_invoke: bool = Field(
        default=False,
        description="Permission to invoke other agents.",
    )
    agent_manage: bool = Field(
        default=False,
        description="Permission to modify agent configurations.",
    )

    # Config operations
    config_read: bool = Field(
        default=True,
        description="Permission to read conductor configuration.",
    )
    config_write: bool = Field(
        default=False,
        description="Permission to modify conductor configuration.",
    )

    # Project operations
    project_register: bool = Field(
        default=False,
        description="Permission to register new projects.",
    )
    project_retire: bool = Field(
        default=False,
        description="Permission to retire/archive projects.",
    )

    # Cron operations
    cron_read: bool = Field(
        default=True,
        description="Permission to view scheduled jobs.",
    )
    cron_manage: bool = Field(
        default=False,
        description="Permission to create, modify, or delete cron jobs.",
    )

    # Autonomy controls
    autonomous_mode: bool = Field(
        default=False,
        description="Work without constant supervision (autopilot/agent mode).",
    )
    require_approval_destructive: bool = Field(
        default=True,
        description="Require user approval for destructive actions (default: safest).",
    )
    trusted_directories_only: bool = Field(
        default=True,
        description="Only operate in trusted directories from trust.json.",
    )

    # Admin operations
    admin_full: bool = Field(
        default=False,
        description="Full administrative access. Only conductor should have this.",
    )

    _restrictive_true_fields: ClassVar[list[str]] = [
        "require_approval_destructive",
        "trusted_directories_only",
    ]

    @classmethod
    def set_permissions(cls, permission: bool, *fields: str) -> "AgentPermissions":
        permissions = (
            {
                **{
                    name: permission
                    for name in fields
                    if cls.model_fields[name].default
                    and name not in cls._restrictive_true_fields
                },
                **{
                    name: not permission
                    for name in fields
                    if name in cls._restrictive_true_fields
                },
            }
            if fields
            else {
                **{
                    name: permission
                    for name, definition in cls.model_fields.items()
                    if definition.default and name not in cls._restrictive_true_fields
                },
                **{field: not permission for field in cls._restrictive_true_fields},
            }
        )

        return cls(**permissions)

    @classmethod
    def all_permissions(cls) -> "AgentPermissions":
        """Return a permissions object with all permissions enabled (for conductor)."""
        return cls.set_permissions(True)

    @classmethod
    def none(cls) -> "AgentPermissions":
        """Return a permissions object with all permissions disabled."""
        return cls.set_permissions(False)

    @classmethod
    def read_only(cls) -> "AgentPermissions":
        """Return a read-only permissions object (safest default)."""
        return cls.set_permissions(
            False,
            "workspace_read",
            "codebase_context",
            "git_read",
            "config_read",
            "cron_read",
        )

    @classmethod
    def developer_default(cls) -> "AgentPermissions":
        """Default permissions for developer agents: coding with safety rails."""
        return cls.set_permissions(
            True,
            "file_create",
            "file_modify",
            "terminal_execute",
            "git_commit",
            "git_branch",
            "git_pr_create",
            "agent_invoke",
        )

    @classmethod
    def reviewer_default(cls) -> "AgentPermissions":
        """Default permissions for reviewer agents: read and comment only."""
        return cls.set_permissions(True, "git_pr_review", "agent_invoke")

    @classmethod
    def git_agent_default(cls) -> "AgentPermissions":
        """Default permissions for git agents: version control operations."""
        return cls.set_permissions(
            True,
            "git_commit",
            "git_branch",
            "git_push",
            "git_merge",
            "git_pr_create",
            "git_pr_review",
            "terminal_execute",
            "agent_invoke",
        )

    @classmethod
    def tester_default(cls) -> "AgentPermissions":
        """Default permissions for tester agents: run tests, read code."""
        return cls.set_permissions(
            True,
            "file_create",
            "file_modify",
            "terminal_execute",
            "terminal_background",
            "git_commit",
            "agent_invoke",
        )

    @classmethod
    def support_default(cls) -> "AgentPermissions":
        """Default permissions for support/dogsbody agents: limited utility tasks."""
        return cls.set_permissions(
            True,
            "file_create",
            "file_modify",
            "terminal_execute",
            "git_commit",
            "agent_invoke",
        )

    @classmethod
    def agile_default(cls) -> "AgentPermissions":
        """Default permissions for agile agents: project management, not code changes."""
        return cls.set_permissions(
            True,
            "file_create",
            "file_modify",
            "git_commit",
            "git_pr_review",
            "agent_invoke",
        )

    @classmethod
    def arbitrator_default(cls) -> "AgentPermissions":
        """Default permissions for arbitrator agents: resolve conflicts, limited actions."""
        return cls.set_permissions(True, "agent_invoke", "agent_manage")

    def to_list(self) -> list[str]:
        """Return list of enabled permission names (True values only)."""
        return [
            field_name
            for field_name in type(self).model_fields
            if getattr(self, field_name) is True
        ]

    def to_enum_list(self) -> list[Permission]:
        """Return list of enabled permissions as Permission enum values."""
        return [Permission(name) for name in self.to_list()]

    @classmethod
    def from_list(cls, permissions: list[str | Permission]) -> "AgentPermissions":
        """Create permissions from a list of permission names or Permission enums.

        Args:
            permissions: List of permission strings or Permission enum values.
                Only permissions in this list will be set to True.
                All other permissions use their field defaults.

        Returns:
            AgentPermissions instance with specified permissions enabled.

        Example:
            >>> AgentPermissions.from_list(["workspace_read", "file_create"])
            >>> AgentPermissions.from_list([Permission.WORKSPACE_READ, Permission.FILE_CREATE])
        """
        data = {}
        for perm in permissions:
            # Convert Permission enum to string value if needed
            perm_str = perm.value if isinstance(perm, Permission) else str(perm)
            if perm_str in cls.model_fields:
                data[perm_str] = True
        return cls(**data)

    @classmethod
    def for_role(cls, role: str) -> "AgentPermissions":
        """Get default permissions for a given role."""
        role_defaults = {
            "conductor": cls.all_permissions,
            "developer": cls.developer_default,
            "reviewer": cls.reviewer_default,
            "git": cls.git_agent_default,
            "tester": cls.tester_default,
            "support": cls.support_default,
            "agile": cls.agile_default,
            "arbitrator": cls.arbitrator_default,
        }
        factory = role_defaults.get(role, cls.read_only)
        return factory()


class AgentConfig(BaseModel):
    """Configuration for a conductor agent.

    Supports all fields from conductor.json agent definitions and can be extended
    with CustomAgentConfig fields when the copilot package is available.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str | None = Field(
        default=None,
        description="Display name for this agent. Defaults to the role key if absent.",
    )

    role: str | None = Field(
        default=None,
        description="The role key from conductor.json (e.g., 'conductor', 'developer').",
    )

    project: str | None = Field(
        default=None,
        description="Project/workspace context for this agent, if applicable.",
    )

    description: str | None = Field(
        default=None, description="Human-readable description of the agent's purpose."
    )
    active: bool = Field(
        default=False, description="Whether the agent is active for this session."
    )
    model: str | None = Field(
        default=None,
        description="Model override for this agent. Falls back to models.default_model if absent.",
    )
    sidekick: bool = Field(
        default=False,
        description="If true, this agent is the designated sidekick for development tasks.",
    )
    augmenting: bool = Field(
        default=False,
        description=(
            "If true, this agent augments the base copilot functionality"
            " rather than operating as a standalone agent."
        ),
    )
    cron: CronConfig | None = Field(
        default=None,
        description=(
            "Cron job configuration."
            " If set, a cron file at .github/agents/cron/<role>.cron will be created."
        ),
    )
    scope: Literal["system", "project"] = Field(
        default="system",
        description="Operational scope: 'system' (global) or 'project' (workspace-scoped).",
    )
    instances: InstancePolicy | None = Field(
        default=None, description="Concurrency policy for multiple agent instances."
    )
    instructions: str | None = Field(
        default=None,
        description="Custom instructions for the agent.",
    )
    permissions: AgentPermissions | None = Field(
        default=None,
        description=(
            "Granular permissions for this agent."
            " Accepts a list of permission names or a dict."
            " If not set, defaults based on role."
        ),
    )
    multiple_instances_naming_pattern: ClassVar[re.Pattern] = re.compile(
        r"(?<=-\[\{)(\w*)(?=\}\])"
    )

    @field_validator("permissions", mode="before")
    @classmethod
    def validate_permissions(
        cls, value: list | dict | AgentPermissions | None
    ) -> AgentPermissions | None:
        """Convert permissions from list or dict format to AgentPermissions.

        Supports three formats:
        1. List of strings: ["workspace_read", "file_create", "git_commit"]
        2. Dict of bools: {"workspace_read": true, "file_create": true}
        3. AgentPermissions instance (passthrough)
        """
        if value is None:
            return None
        if isinstance(value, AgentPermissions):
            return value
        if isinstance(value, list):
            return AgentPermissions.from_list(value)
        if isinstance(value, dict):
            return AgentPermissions(**value)
        raise ValueError(
            f"permissions must be a list, dict, or AgentPermissions, got {type(value)}"
        )

    def set_role(self, role: str) -> Self:
        """Set the role and return self for chaining."""
        self.role = role
        if self.name is None:
            self.name = role
        return self

    def get_permissions(self) -> AgentPermissions:
        """Get permissions for this agent, falling back to role defaults."""
        if self.permissions is not None:
            return self.permissions
        return AgentPermissions.for_role(self.role or "")

    def is_conductor(self) -> bool:
        """Check if this is the conductor agent."""
        return self.role == "conductor"

    @staticmethod
    def _get_name_keys(name: str) -> list[str]:
        """Extract the instance key from the agent name using the naming convention."""
        return re.findall(AgentConfig.multiple_instances_naming_pattern, name)

    @classmethod
    def create_multi(cls, info: dict) -> list["AgentConfig"]:
        """Create multiple agent instances from a config with instance policy."""
        name_keys = cls._get_name_keys(info.get("name", ""))
        if not name_keys or "rank" not in name_keys:
            raise AgentNamingPatternException(
                f"Agent name must contain a pattern for multiple instances: {info.get('name')}"
            )

        if "role" not in info:
            raise ValueError(
                "Agent config must include a 'role' key for multiple instances."
            )

        instances = info.get("instances")
        if isinstance(instances, dict):
            instances = InstancePolicy(**instances)
        if not isinstance(instances, (InstancePolicy, type(None))):
            raise ValueError(
                "Agent config with multiple instances must include an 'instances' key"
                " with an InstancePolicy value."
            )

        if "project" in name_keys and "project" not in info:
            raise AgentNamingPatternException(
                "Agent name pattern includes 'project' but no value for project was provided."
            )

        if "project" in name_keys and info.get("project") is None:
            raise ValueError("If provided, 'project' must be a non-empty string.")

        agents = []
        for rank in instances or range(1, 2):
            agent_info = info.copy()
            agent_info["name"] = agent_info["name"].replace("{rank}", str(rank))
            if "project" in name_keys and "project" in info:
                agent_info["name"] = agent_info["name"].replace(
                    "{project}", str(info["project"])
                )
            # Don't pass instances dict to individual agents
            agent_info.pop("instances", None)
            agents.append(cls(**agent_info))

        return agents


class ModelsConfig(BaseModel):
    """Model configuration for the conductor system."""

    authorized_models: list[str] = Field(
        ...,
        min_length=1,
        description="List of model identifiers agents are permitted to use.",
    )
    default_model: str = Field(
        ..., description="Default model when an agent does not specify its own."
    )


class ConductorConfig(BaseModel):
    """Main configuration model for the conductor system."""

    model_config = {"populate_by_name": True}

    version: VersionConfig | None = Field(
        default=None,
        description="Version information for this configuration.",
    )
    models: ModelsConfig = Field(..., description="Model configuration settings.")
    agents: dict[str, AgentConfig] = Field(
        default_factory=dict,
        alias="agent",
        description="Mapping of agent role keys to their configurations.",
    )
    tasks: list[TaskConfig] = Field(
        default_factory=list,
        description="Top-level task definitions. Only tasks with cron expressions are scheduled.",
    )

    @field_validator("agents", mode="before")
    @classmethod
    def validate_agents(cls, agents: dict[str, dict]) -> dict[str, AgentConfig]:
        """Convert raw agent dicts to AgentConfig instances and validate them."""
        validated_agents = {}
        for role, config in agents.items():
            if not isinstance(config, AgentConfig):
                try:
                    agent_config = AgentConfig(**config).set_role(role)
                except Exception as e:
                    raise ValueError(
                        f"Invalid configuration for agent '{role}': {e}"
                    ) from e
            else:
                agent_config = config
            validated_agents[role] = agent_config
        return validated_agents

    @property
    def agent_dicts(self) -> dict[str, dict]:
        """Return agent configs as raw dicts for backward compatibility."""
        return {
            role: cfg.model_dump(exclude_none=True) for role, cfg in self.agents.items()
        }

    def get_agent_dict(self, role: str) -> dict:
        """Get a single agent's config as a dict, or empty dict if not found."""
        agent = self.agents.get(role)
        return agent.model_dump(exclude_none=True) if agent else {}

    @property
    def scheduled_tasks(self) -> list[TaskConfig]:
        """Return only tasks that have a cron expression (are scheduled)."""
        return [task for task in self.tasks if task.cron is not None]

    @property
    def manual_tasks(self) -> list[TaskConfig]:
        """Return only tasks without a cron expression (triggered manually)."""
        return [task for task in self.tasks if task.cron is None]

    def get_tasks_for_agent(self, agent_role: str) -> list[TaskConfig]:
        """Return all tasks assigned to a specific agent role."""
        return [task for task in self.tasks if task.agent == agent_role]

    def can_agent_run_task(self, task: TaskConfig) -> tuple[bool, list[str]]:
        """Check if the agent has permissions to run a task.

        Args:
            task: The task to check permissions for.

        Returns:
            Tuple of (can_run, missing_permissions).
            If can_run is False, missing_permissions lists what's lacking.
        """
        agent = self.agents.get(task.agent)
        if not agent:
            return False, [f"Agent '{task.agent}' not found"]

        if not agent.active:
            return False, [f"Agent '{task.agent}' is not active"]

        if not task.permissions:
            return True, []

        agent_perms = agent.get_permissions()
        agent_perm_list = agent_perms.to_list()

        missing = [p for p in task.permissions if p not in agent_perm_list]
        return len(missing) == 0, missing


class Agent(AgentConfig):
    """Runtime agent model enriched with live status from Copilot SDK sessions."""

    running: bool = Field(
        default=False,
        description="True if an SDK-backed session for this agent is currently running.",
    )

    _running_checker: ClassVar[Callable[[str], bool] | None] = None

    @classmethod
    def set_running_checker(cls, checker: Callable[[str], bool] | None) -> None:
        """
        Register a callback used to determine whether an agent is currently running.

        Example:
            Agent.set_running_checker(sdk.is_running)

        Note:
            Pass ``None`` to clear the registered checker.

        :param checker: callable that returns ``True`` when the agent named
            by its argument has a live SDK session, or ``None`` to disable
            the check.
        :type checker: `Callable[[str], bool] | None`
        :return: None
        :rtype: `None`
        """
        cls._running_checker = checker

    def _compute_running(self) -> bool:
        """Compute running status using the registered SDK checker."""
        checker = self._running_checker
        if checker is None:
            return False
        name = self.name or self.role or ""
        try:
            return bool(checker(name))
        except Exception:
            return False

    @model_validator(mode="after")
    def compute_running(self) -> Agent:
        """
        Refresh ``running`` after Pydantic validation completes.

        :return: this instance with ``running`` set from the registered checker.
        :rtype: `Agent`
        """
        self.running = self._compute_running()
        return self

    def set_role(self, role: str) -> Self:
        """
        Set the agent role and recompute ``running`` for the new identity.

        :param role: role identifier to assign.
        :type role: `str`
        :return: this instance with the updated role and running flag.
        :rtype: `Self`
        """
        super().set_role(role)
        self.running = self._compute_running()
        return self


class Conductor(ConductorConfig):
    """Singleton runtime config whose agents are materialized as ``Agent`` models."""

    _instance: ClassVar[Conductor | None] = None

    @model_validator(mode="after")
    def upgrade_agents(self) -> Conductor:
        """
        Ensure every entry in :attr:`agents` is a runtime :class:`Agent`.

        Note:
            Existing :class:`Agent` instances are left untouched; bare
            :class:`AgentConfig` entries are re-validated as :class:`Agent`.

        :return: this instance with the ``agents`` mapping upgraded in place.
        :rtype: `Conductor`
        """
        upgraded: dict[str, AgentConfig] = {}
        for role, cfg in self.agents.items():
            if isinstance(cfg, Agent):
                upgraded[role] = cfg
            else:
                upgraded[role] = Agent.model_validate(cfg.model_dump()).set_role(role)
        self.agents = upgraded
        return self

    @classmethod
    def instance(cls, data: dict[str, Any] | None = None) -> Conductor:
        """
        Return the singleton :class:`Conductor`, creating or refreshing it.

        Example:
            conductor = Conductor.instance(config_dict)

        Note:
            On first call ``data`` is required to build the singleton; on
            subsequent calls an optional ``data`` mapping refreshes the
            existing instance's fields in place.

        :param data: configuration dictionary used to construct or refresh
            the singleton.
        :type data: `dict[str, Any] | None`
        :return: the singleton :class:`Conductor`.
        :rtype: `Conductor`
        :raises RuntimeError: when called for the first time with ``data``
            set to ``None``.
        """
        if cls._instance is None:
            if data is None:
                raise RuntimeError("Conductor singleton not initialized")
            cls._instance = cls(**data)
            return cls._instance

        if data is not None:
            refreshed = cls(**data)
            for field_name in cls.model_fields:
                setattr(cls._instance, field_name, getattr(refreshed, field_name))
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """
        Discard the singleton instance.

        Note:
            Primarily used by tests and reload paths.

        :return: None
        :rtype: `None`
        """
        cls._instance = None
