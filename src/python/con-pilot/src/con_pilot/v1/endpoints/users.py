"""POST /v1/create-user endpoint — admin-only, authenticated by the install key."""

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from con_pilot.users import create_user

log = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

# ---------------------------------------------------------------------------
# Install-key authentication
# ---------------------------------------------------------------------------

_install_key_header = APIKeyHeader(name="X-Install-Key", auto_error=False)


def _read_install_key() -> bytes:
    """Return the raw bytes of the install-time key file."""
    conductor_home = os.environ.get("CONDUCTOR_HOME", "")
    key_file = os.path.join(conductor_home, "key") if conductor_home else "key"
    if not os.path.exists(key_file):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Install key not found on this server",
        )
    with open(key_file, "rb") as f:
        return f.read().strip()


def _require_install_key(api_key: str | None = Security(_install_key_header)) -> str:
    """FastAPI dependency — validates the X-Install-Key header against the key file."""
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Install-Key header is required",
        )
    expected = _read_install_key()
    # Use constant-time comparison to avoid timing attacks
    import hmac

    if not hmac.compare_digest(api_key.encode(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid install key",
        )
    return api_key


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8)
    active: bool = True

    @field_validator("username")
    @classmethod
    def _no_spaces(cls, v: str) -> str:
        if " " in v:
            raise ValueError("username must not contain spaces")
        return v


class CreateUserResponse(BaseModel):
    username: str
    active: bool
    created: bool = True


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/create-user",
    response_model=CreateUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(_require_install_key)],
)
def create_user_endpoint(body: CreateUserRequest) -> CreateUserResponse:
    """
    Create a new user.

    Requires the ``X-Install-Key`` header to be set to the exact contents of
    the ``$CONDUCTOR_HOME/key`` file generated at install time.
    """
    try:
        create_user(username=body.username, password=body.password, active=body.active)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    return CreateUserResponse(username=body.username, active=body.active)
