"""POST /v1/create-user endpoint — admin-only, authenticated by the install key."""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from con_pilot.logger import app_logger
from con_pilot.paths import resolve_key_file
from con_pilot.users import create_user

log = app_logger.bind(module=__name__)

router = APIRouter(tags=["admin"])

# Dev builds (CONDUCTOR_ENV=DEV) accept this null GUID instead of a generated key.
_DEV_NULL_KEY = b"00000000-0000-0000-0000-000000000000"


def _is_dev_build() -> bool:
    return os.environ.get("CONDUCTOR_ENV") == "DEV"


# ---------------------------------------------------------------------------
# Install-key authentication
# ---------------------------------------------------------------------------

_install_key_header = APIKeyHeader(name="X-Install-Key", auto_error=False)


def _read_install_key() -> bytes:
    """Return the raw bytes of the install-time key file.

    Dev builds short-circuit to the null GUID and never touch the on-disk
    key file (which may carry a stale uuid4 written by the legacy installer).
    """
    if _is_dev_build():
        return _DEV_NULL_KEY
    key_file = resolve_key_file(os.environ.get("CONDUCTOR_HOME", ""))
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


class VerifyKeyRequest(BaseModel):
    key: str = Field(..., min_length=1)


class VerifyKeyResponse(BaseModel):
    valid: bool


class ShowMeResponse(BaseModel):
    enabled: bool
    conductor_home: str
    appdir: str
    key_file: str
    key_value: str
    appdir_entries: list[str]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/verify-key",
    response_model=VerifyKeyResponse,
    status_code=status.HTTP_200_OK,
)
def verify_key_endpoint(body: VerifyKeyRequest) -> VerifyKeyResponse:
    """Verify that the provided install key exactly matches the server key."""
    expected = _read_install_key()
    import hmac

    if not hmac.compare_digest(body.key.encode(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid install key",
        )
    return VerifyKeyResponse(valid=True)


def _dev_show_me_enabled() -> bool:
    return os.environ.get("CON_PILOT_DEV_SHOW_ME", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _list_appdir_entries(appdir: str, limit: int = 200) -> list[str]:
    if not appdir or not os.path.isdir(appdir):
        return []

    base = Path(appdir)
    entries: list[str] = []
    for path in sorted(base.rglob("*")):
        if len(entries) >= limit:
            entries.append("...truncated...")
            break
        suffix = "/" if path.is_dir() else ""
        entries.append(f"{path.relative_to(base)}{suffix}")
    return entries


@router.get(
    "/show-me",
    response_model=ShowMeResponse,
    status_code=status.HTTP_200_OK,
)
def show_me_endpoint() -> ShowMeResponse:
    """Temporary dev-only endpoint for key and AppImage/AppDir diagnostics."""
    if not _dev_show_me_enabled():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    conductor_home = os.environ.get("CONDUCTOR_HOME", "")
    appdir = os.environ.get("APPDIR", "")
    key_file = resolve_key_file(conductor_home)
    try:
        with open(key_file, "rb") as f:
            key_value = f.read().strip().decode("utf-8", errors="replace")
    except OSError:
        key_value = ""

    return ShowMeResponse(
        enabled=True,
        conductor_home=conductor_home,
        appdir=appdir,
        key_file=key_file,
        key_value=key_value,
        appdir_entries=_list_appdir_entries(appdir),
    )


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
