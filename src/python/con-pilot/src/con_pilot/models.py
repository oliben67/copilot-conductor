import re
from typing import ClassVar, Literal, Self

from pydantic import BaseModel, Field, field_validator, model_validator


class AgentNamingPatternException(Exception):
    """Custom exception for invalid agent naming patterns."""

    pass


class InstancePolicy(BaseModel):
    """Concurrency policy: minimum and optional maximum number of agent instances."""

    min: int = Field(default=1, ge=1, description="Minimum number of instances that must be running.")
    max: int | None = Field(default=None, ge=1, description="Maximum concurrent instances allowed. None for no limit.")

    def __iter__(self):
        """Iterate over instance numbers from min to max (inclusive)."""
        for i in range(self.min, (self.max or self.min) + 1):
            yield i


class AgentConfig(BaseModel):
    """Configuration for a conductor agent.

    Supports all fields from conductor.json agent definitions and can be extended
    with CustomAgentConfig fields when the copilot package is available.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str | None = Field(
        default=None,
        description="Display name for this agent. Defaults to the role key if absent."
    )

    role: str | None = Field(
        default=None,
        description="The role key from conductor.json (e.g., 'conductor', 'developer')."
    )

    project: str | None = Field(
        default=None,
        description="Project/workspace context for this agent, if applicable.",
    )

    description: str | None = Field(
        default=None,
        description="Human-readable description of the agent's purpose."
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
    has_cron_jobs: bool = Field(
        default=False,
        description="If true, a cron file at .github/agents/cron/<role>.cron is expected.",
    )
    scope: Literal["system", "project"] = Field(
        default="system",
        description="Operational scope: 'system' (global) or 'project' (workspace-scoped).",
    )
    instances: InstancePolicy | None = Field(
        default=None, description="Concurrency policy for multiple agent instances."
    )
    instructions: str | None = Field(
        default=None, description="Custom instructions for the agent."
    )
    multiple_instances_naming_pattern: ClassVar[re.Pattern] = re.compile(
        r"(?<=-\[\{)(\w*)(?=\}\])"
    )

    def set_role(self, role: str) -> Self:
        """Set the role and return self for chaining."""
        self.role = role
        if self.name is None:
            self.name = role
        return self

    @staticmethod
    def _get_name_keys(name: str) -> list[str]:
        """Extract the instance key from the agent name using the naming convention."""
        return re.findall(AgentConfig.multiple_instances_naming_pattern, name)

    @classmethod
    def create_multi(cls, info: dict) -> list["AgentConfig"]:
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
                "Agent config with multiple instances must include an 'instances' key with an InstancePolicy value."
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

    models: ModelsConfig = Field(..., description="Model configuration settings.")
    agents: dict[str, AgentConfig] = Field(
        default_factory=dict,
        alias="agent",
        description="Mapping of agent role keys to their configurations.",
    )

    @field_validator("agents", mode="before")
    def validate_agents(cls, agents: dict[str, dict]) -> dict[str, AgentConfig]:
        """Convert raw agent dicts to AgentConfig instances and validate them."""
        validated_agents = {}
        for role, config in agents.items():
            if not isinstance(config, AgentConfig):
                try:
                    agent_config = AgentConfig(**config).set_role(role)
                except Exception as e:
                    raise ValueError(f"Invalid configuration for agent '{role}': {e}")
            else:
                agent_config = config
            validated_agents[role] = agent_config
        return validated_agents

    @property
    def agent_dicts(self) -> dict[str, dict]:
        """Return agent configs as raw dicts for backward compatibility."""
        return {role: cfg.model_dump(exclude_none=True) for role, cfg in self.agents.items()}

    def get_agent_dict(self, role: str) -> dict:
        """Get a single agent's config as a dict, or empty dict if not found."""
        agent = self.agents.get(role)
        return agent.model_dump(exclude_none=True) if agent else {}


class AgentInfo(BaseModel):
    """Information about a loaded agent for the list-agents response."""

    role: str = Field(..., description="The agent's role key (e.g., 'conductor', 'developer').")
    name: str = Field(..., description="The resolved display name of the agent.")
    scope: Literal["system", "project"] = Field(..., description="Operational scope.")
    active: bool = Field(..., description="Whether the agent is active.")
    file_exists: bool = Field(..., description="Whether the .agent.md file exists.")
    file_path: str | None = Field(default=None, description="Path to the .agent.md file if it exists.")
    project: str | None = Field(default=None, description="Project name if project-scoped.")
    instance: int | None = Field(default=None, description="Instance number for multi-instance agents.")
    sidekick: bool = Field(default=False, description="Whether this is the sidekick agent.")
    model: str | None = Field(default=None, description="Model override, if any.")
    description: str | None = Field(default=None, description="Agent description.")


class AgentListResponse(BaseModel):
    """Response model for the list-agents endpoint."""

    system_agents: list[AgentInfo] = Field(default_factory=list, description="System-scoped agents.")
    project_agents: list[AgentInfo] = Field(default_factory=list, description="Project-scoped agents.")


class ValidationError(BaseModel):
    """A single validation error."""

    path: str = Field(..., description="JSON path where the error occurred (e.g., '$.agent.conductor').")
    message: str = Field(..., description="Human-readable description of the error.")
    validator: str = Field(default="unknown", description="Name of the validator that failed.")


class ValidationResult(BaseModel):
    """Result of validating conductor.json against the schema."""

    valid: bool = Field(..., description="Whether the configuration is valid.")
    errors: list[ValidationError] = Field(default_factory=list, description="List of validation errors.")
    warnings: list[str] = Field(default_factory=list, description="Non-fatal warnings.")
    config_path: str | None = Field(default=None, description="Path to the validated config file.")
    schema_path: str | None = Field(default=None, description="Path to the schema file used.")
