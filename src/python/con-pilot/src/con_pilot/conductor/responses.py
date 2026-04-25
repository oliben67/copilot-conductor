"""Response models for API endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from con_pilot.conductor.models import CronConfig, InstancePolicy, TaskConfig


class AgentInfo(BaseModel):
    """Information about a loaded agent for the list-agents response."""

    role: str = Field(
        ..., description="The agent's role key (e.g., 'conductor', 'developer')."
    )
    name: str = Field(..., description="The resolved display name of the agent.")
    scope: Literal["system", "project"] = Field(..., description="Operational scope.")
    active: bool = Field(..., description="Whether the agent is active.")
    file_exists: bool = Field(..., description="Whether the .agent.md file exists.")
    file_path: str | None = Field(
        default=None, description="Path to the .agent.md file if it exists."
    )
    project: str | None = Field(
        default=None, description="Project name if project-scoped."
    )
    instance: int | None = Field(
        default=None, description="Instance number for multi-instance agents."
    )
    sidekick: bool = Field(
        default=False, description="Whether this is the sidekick agent."
    )
    augmenting: bool = Field(
        default=False,
        description="Whether this agent augments base copilot functionality.",
    )
    model: str | None = Field(default=None, description="Model override, if any.")
    description: str | None = Field(default=None, description="Agent description.")
    running: bool = Field(
        default=False,
        description="Whether the runtime session for this agent is currently running.",
    )


class AgentListResponse(BaseModel):
    """Response model for the list-agents endpoint."""

    system_agents: list[AgentInfo] = Field(
        default_factory=list, description="System-scoped agents."
    )
    project_agents: list[AgentInfo] = Field(
        default_factory=list, description="Project-scoped agents."
    )


class AgentDetailResponse(AgentInfo):
    """Detailed information about a single agent, including permissions and tasks."""

    permissions: list[str] = Field(
        default_factory=list,
        description="List of enabled permission names for this agent.",
    )
    tasks: list[TaskConfig] = Field(
        default_factory=list,
        description="Tasks assigned to this agent.",
    )
    cron: CronConfig | None = Field(
        default=None,
        description="The agent's own cron schedule configuration, if any.",
    )
    instances: InstancePolicy | None = Field(
        default=None,
        description="Instance concurrency policy, if applicable.",
    )
    instructions: str | None = Field(
        default=None,
        description="Custom instructions for the agent.",
    )

    model_config = {"arbitrary_types_allowed": True}


class ValidationError(BaseModel):
    """A single validation error."""

    path: str = Field(
        ...,
        description="JSON path where the error occurred (e.g., '$.agent.conductor').",
    )
    message: str = Field(..., description="Human-readable description of the error.")
    validator: str = Field(
        default="unknown", description="Name of the validator that failed."
    )


class ValidationResult(BaseModel):
    """Result of validating conductor.json against the schema."""

    valid: bool = Field(..., description="Whether the configuration is valid.")
    errors: list[ValidationError] = Field(
        default_factory=list, description="List of validation errors."
    )
    warnings: list[str] = Field(default_factory=list, description="Non-fatal warnings.")
    config_path: str | None = Field(
        default=None, description="Path to the validated config file."
    )
    schema_path: str | None = Field(
        default=None, description="Path to the schema file used."
    )
