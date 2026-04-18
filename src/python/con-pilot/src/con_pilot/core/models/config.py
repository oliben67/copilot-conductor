"""Configuration models for conductor.json parsing and validation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Literal, Self

from croniter import croniter
from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    pass

from con_pilot.exceptions import AgentNamingPatternException


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
        description="Custom cron file path relative to .github/agents/cron/. Defaults to <role>.cron if absent.",
    )

    @field_validator("expression")
    @classmethod
    def validate_cron_expression(cls, v: str) -> str:
        """Validate the cron expression using croniter."""
        if not croniter.is_valid(v):
            raise ValueError(f"Invalid cron expression: {v}")
        return v


class InstancePolicy(BaseModel):
    """Concurrency policy: minimum and optional maximum number of agent instances."""

    min: int = Field(
        default=1, ge=1, description="Minimum number of instances that must be running."
    )
    max: int | None = Field(
        default=None,
        ge=1,
        description="Maximum concurrent instances allowed. None for no limit.",
    )

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
        description="If true, this agent augments the base copilot functionality rather than operating as a standalone agent.",
    )
    cron: CronConfig | None = Field(
        default=None,
        description="Cron job configuration. If set, a cron file at .github/agents/cron/<role>.cron will be created.",
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
                    raise ValueError(f"Invalid configuration for agent '{role}': {e}")
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
