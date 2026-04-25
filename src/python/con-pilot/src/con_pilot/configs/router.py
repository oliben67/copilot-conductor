"""Configuration version management endpoints."""

from typing import Any

from con_pilot.conductor import ConPilot
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from con_pilot.conductor.models import ConductorConfig
from con_pilot.conductor.responses import ValidationResult
from con_pilot.conductor.config_store import (
    ConfigVersion,
    VersionExistsError,
    VersionNotFoundError,
)

router = APIRouter(prefix="/config", tags=["config"])


def get_pilot() -> ConPilot:
    """Dependency to get the ConPilot instance."""
    from con_pilot.app import get_pilot as _get_pilot

    return _get_pilot()


# ── Request/Response Models ────────────────────────────────────────────────────


class ConfigListResponse(BaseModel):
    """Response for listing all stored configurations."""

    versions: list[ConfigVersion] = Field(
        ..., description="List of stored config versions"
    )
    active_version: str | None = Field(
        None, description="Currently active config version"
    )
    scores_dir: str = Field(..., description="Path to the .scores directory")


class ConfigCreateRequest(BaseModel):
    """Request body for creating a new configuration."""

    config: dict[str, Any] = Field(
        ..., description="The conductor configuration as a dict"
    )


class ConfigCreateResponse(BaseModel):
    """Response for successful config creation."""

    message: str
    version: ConfigVersion
    validation: ValidationResult | None = None


class ConfigDiffRequest(BaseModel):
    """Request body for generating a diff."""

    version_a: str = Field(..., description="First version (older)")
    version_b: str = Field(..., description="Second version (newer)")
    context_lines: int = Field(default=3, ge=0, description="Context lines in diff")


class ConfigDiffResponse(BaseModel):
    """Response containing the diff between two versions."""

    version_a: str
    version_b: str
    diff: str


class ConfigActivateRequest(BaseModel):
    """Request body for activating a version."""

    restart: bool = Field(
        default=True, description="Whether to trigger a service restart"
    )


class ConfigActivateResponse(BaseModel):
    """Response for successful version activation."""

    message: str
    version: str
    config_path: str


class ConfigErrorResponse(BaseModel):
    """Error response with details."""

    error: str
    detail: str | None = None


# ── Public Endpoints (no auth required) ────────────────────────────────────────


@router.get("", response_model=ConfigListResponse)
def list_configs(pilot: ConPilot = Depends(get_pilot)) -> ConfigListResponse:
    """
    List all stored configuration versions.

    Returns metadata for each version including timestamps and descriptions.
    """
    pilot = pilot or get_pilot()
    store = pilot.config_store

    # Get active config version if available
    active_version: str | None = None
    try:
        if pilot.config.version:
            active_version = pilot.config.version.number
    except Exception:  # noqa: BLE001, S110
        pass

    return ConfigListResponse(
        versions=store.versions,
        active_version=active_version,
        scores_dir=store.scores_dir,
    )


@router.get("/{version}", response_model=dict)
def get_config(version: str, pilot: ConPilot = Depends(get_pilot)) -> dict:
    """
    Get a specific configuration version.

    Returns the full configuration as a dict.
    """
    pilot = pilot or get_pilot()
    store = pilot.config_store

    try:
        config = store.get_or_raise(version)
        return config.model_dump(mode="json", by_alias=True, exclude_none=True)
    except VersionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e


@router.post("/diff", response_model=ConfigDiffResponse)
def diff_configs(
    body: ConfigDiffRequest,
    pilot: ConPilot = Depends(get_pilot),
) -> ConfigDiffResponse:
    """
    Generate a unified diff between two configuration versions.

    Both versions must exist in the .scores directory.
    """
    pilot = pilot or get_pilot()
    store = pilot.config_store

    try:
        diff_text = store.diff(
            body.version_a,
            body.version_b,
            context_lines=body.context_lines,
        )
        return ConfigDiffResponse(
            version_a=body.version_a,
            version_b=body.version_b,
            diff=diff_text,
        )
    except VersionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e


@router.get("/{version}/diff-with-active", response_model=ConfigDiffResponse)
def diff_with_active(
    version: str,
    context_lines: int = 3,
    pilot: ConPilot = Depends(get_pilot),
) -> ConfigDiffResponse:
    """
    Generate a diff between a stored version and the active configuration.
    """
    pilot = pilot or get_pilot()
    store = pilot.config_store

    try:
        diff_text = store.diff_with_active(version, context_lines=context_lines)
        return ConfigDiffResponse(
            version_a=version,
            version_b="active",
            diff=diff_text,
        )
    except VersionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e


@router.post(
    "", response_model=ConfigCreateResponse, status_code=status.HTTP_201_CREATED
)
def create_config(
    body: ConfigCreateRequest,
    pilot: ConPilot = Depends(get_pilot),
) -> ConfigCreateResponse:
    """
    Create a new configuration version.

    The configuration must include valid version info. If the version number
    already exists, returns an error with the existing version's details.

    Validates the configuration against the schema before saving.
    """
    pilot = pilot or get_pilot()
    store = pilot.config_store

    # Parse and validate the config
    try:
        config = ConductorConfig(**body.config)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid configuration: {e}",
        ) from e

    # Check for version field
    if not config.version:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Configuration must include version info with 'version.number' field",
        )

    # Perform schema validation
    import os
    import tempfile

    import yaml as _yaml

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        _yaml.safe_dump(
            config.model_dump(mode="json", by_alias=True, exclude_none=True),
            tmp,
            default_flow_style=False,
        )
        tmp_path = tmp.name

    try:
        validation_result = pilot.validate(config_path=tmp_path)
    finally:
        os.unlink(tmp_path)

    if not validation_result.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Configuration validation failed",
                "errors": [e.model_dump() for e in validation_result.errors],
            },
        )

    # Try to save
    try:
        version_meta = store.save(config, allow_overwrite=False)
    except VersionExistsError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(e),
                "existing_version": e.existing.model_dump(mode="json"),
            },
        ) from e

    return ConfigCreateResponse(
        message=f"Configuration version {config.version.number} created successfully",
        version=version_meta,
        validation=validation_result,
    )


# ── Admin Endpoints (require system key) ───────────────────────────────────────


def verify_admin_key(
    x_admin_key: str | None = Header(None),
    pilot: ConPilot = Depends(get_pilot),
) -> ConPilot:
    """Verify the admin key header matches the system key."""
    pilot = pilot or get_pilot()
    if not x_admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin key required. Provide X-Admin-Key header.",
        )

    expected_key = pilot._load_or_generate_key()
    if x_admin_key != expected_key:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid admin key",
        )

    return pilot


@router.put(
    "/{version}",
    response_model=ConfigCreateResponse,
    dependencies=[Depends(verify_admin_key)],
)
def update_config(
    version: str,
    body: ConfigCreateRequest,
    pilot: ConPilot = Depends(get_pilot),
) -> ConfigCreateResponse:
    """
    Update an existing configuration version (requires admin key).

    Allows overwriting an existing version. The version number in the request
    body must match the URL parameter.
    """
    pilot = pilot or get_pilot()

    store = pilot.config_store

    # Parse and validate the config
    try:
        config = ConductorConfig(**body.config)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid configuration: {e}",
        ) from e

    # Check for version field and match
    if not config.version:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Configuration must include version info",
        )

    if config.version.number != version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Version mismatch: URL says {version}, body says {config.version.number}",
        )

    # Perform schema validation
    import os
    import tempfile

    import yaml as _yaml

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
        _yaml.safe_dump(
            config.model_dump(mode="json", by_alias=True, exclude_none=True),
            tmp,
            default_flow_style=False,
        )
        tmp_path = tmp.name

    try:
        validation_result = pilot.validate(config_path=tmp_path)
    finally:
        os.unlink(tmp_path)

    if not validation_result.valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "message": "Configuration validation failed",
                "errors": [e.model_dump() for e in validation_result.errors],
            },
        )

    # Save with overwrite allowed
    version_meta = store.save(config, allow_overwrite=True)

    return ConfigCreateResponse(
        message=f"Configuration version {version} updated successfully",
        version=version_meta,
        validation=validation_result,
    )


@router.post(
    "/{version}/activate",
    response_model=ConfigActivateResponse,
    dependencies=[Depends(verify_admin_key)],
)
def activate_config(
    version: str,
    body: ConfigActivateRequest | None = None,
    pilot: ConPilot = Depends(get_pilot),
) -> ConfigActivateResponse:
    """
    Activate a stored configuration version (requires admin key).

    This will:
    1. Backup the current active configuration to .scores
    2. Copy the selected version to conductor.yaml
    3. Optionally trigger a service restart
    """
    pilot = pilot or get_pilot()
    store = pilot.config_store

    try:
        # Activate the version (this backups current and copies new)
        store.activate(version)
    except VersionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e

    # Reload the pilot's config
    pilot.reload_config()

    message = f"Configuration version {version} activated"
    if body and body.restart:
        # Trigger sync to pick up new config
        try:
            pilot.sync()
            message += " and service resynced"
        except Exception as e:  # noqa: BLE001
            message += f" (sync failed: {e})"

    return ConfigActivateResponse(
        message=message,
        version=version,
        config_path=pilot.config_path,
    )


@router.delete(
    "/{version}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(verify_admin_key)],
)
def delete_config(version: str, pilot: ConPilot = Depends(get_pilot)) -> None:
    """
    Delete a stored configuration version (requires admin key).

    Cannot delete the currently active version.
    """
    pilot = pilot or get_pilot()
    store = pilot.config_store

    # Check if this is the active version
    try:
        if pilot.config.version and pilot.config.version.number == version:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot delete the currently active configuration version",
            )
    except Exception:  # noqa: BLE001, S110
        pass

    try:
        store.delete(version)
    except VersionNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        ) from e
