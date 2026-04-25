"""Request body schemas for API endpoints."""

from pydantic import BaseModel, Field


class RegisterRequest(BaseModel):
    """Request body for registering a new project."""

    name: str = Field(..., description="Project name (e.g., my-app).")
    directory: str = Field(
        ..., description="Absolute path to the project root directory."
    )


class RetireProjectRequest(BaseModel):
    """Request body for retiring a project."""

    name: str = Field(..., description="Project name to retire.")


class ReplaceRequest(BaseModel):
    """Request body for replacing an agent's content."""

    file: str = Field(..., description="Path to the instructions file.")
    role: str = Field(..., description="Agent role type (e.g., developer, reviewer).")
    project: str | None = Field(default=None, description="Project name (optional).")
    key: str | None = Field(
        default=None,
        description="System key required when editing system-scoped agents.",
    )


class ResetRequest(BaseModel):
    """Request body for resetting an agent to template."""

    role: str = Field(..., description="Agent role type (e.g., developer, reviewer).")
    project: str | None = Field(default=None, description="Project name (optional).")
    key: str | None = Field(
        default=None,
        description="System key required when editing system-scoped agents.",
    )


class ValidateRequest(BaseModel):
    """Request body for validation endpoint."""

    config_path: str | None = Field(
        default=None,
        description="Path to config file to validate. Defaults to $CONDUCTOR_HOME/conductor.json.",
    )
