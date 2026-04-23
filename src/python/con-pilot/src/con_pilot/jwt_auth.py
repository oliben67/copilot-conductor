"""
jwt_auth.py — JWT signing, verification, and credential checking.

The JWT secret is resolved from (in priority order):
  1. CON_PILOT_JWT_SECRET env var
  2. Contents of $CONDUCTOR_HOME/key file
  3. Auto-generated secret written to $CONDUCTOR_HOME/key on first use

Credentials for username/password login:
  - CON_PILOT_USERNAME  (default: "admin")
  - CON_PILOT_PASSWORD  (required; no default for security)
"""

import base64
import os
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from con_pilot.logger import app_logger
from con_pilot.paths import resolve_key_file

log = app_logger.bind(module=__name__)

_TOKEN_EXPIRY_SECONDS = 3600  # 1 hour

_jwk: jwt.jwk.AbstractJWKBase | None = None


def _load_or_create_key(key_file: str) -> bytes:
    """Load key bytes from file, or generate and persist a new one."""
    if os.path.exists(key_file):
        with open(key_file, "rb") as f:
            data = f.read().strip()
            if data:
                return data
    # Generate a 256-bit random secret and persist it
    raw = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(raw)
    os.makedirs(os.path.dirname(key_file) or ".", exist_ok=True)
    with open(key_file, "wb") as f:
        f.write(encoded)
    os.chmod(key_file, 0o600)
    log.info("Generated new JWT signing key at %s", key_file)
    return encoded


def get_jwk(key_file: str | None = None) -> jwt.jwk.AbstractJWKBase:
    """Return the cached OctetJWK, initialising it on first call."""
    global _jwk  # noqa: PLW0603
    if _jwk is not None:
        return _jwk

    # Priority 1: explicit env var
    secret_env = os.environ.get("CON_PILOT_JWT_SECRET")
    if secret_env:
        key_bytes = secret_env.encode()
    else:
        # Priority 2/3: key file (load or create)
        if key_file is None:
            key_file = resolve_key_file(os.environ.get("CONDUCTOR_HOME", ""))
        key_bytes = _load_or_create_key(key_file)

    _jwk = jwt.jwk.OctetJWK(key_bytes)
    return _jwk


def issue_token(subject: str, extra: dict | None = None) -> tuple[str, int]:
    """
    Sign and return a new JWT for *subject*.

    Returns
    -------
    (token, expires_in_seconds)
    """
    now = datetime.now(UTC)
    payload: dict = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=_TOKEN_EXPIRY_SECONDS)).timestamp()),
    }
    if extra:
        payload.update(extra)

    jwk = get_jwk()
    j = jwt.JWT()
    token = j.encode(payload, jwk, alg="HS256")
    return token, _TOKEN_EXPIRY_SECONDS


def verify_token(token: str) -> dict:
    """
    Verify *token* and return its claims.

    Raises
    ------
    jwt.exceptions.JWTDecodeError / JWTExpiredError on invalid/expired tokens.
    """
    jwk = get_jwk()
    j = jwt.JWT()
    return j.decode(token, jwk)


def check_credentials(username: str, password: str) -> bool:
    """Validate username/password against CON_PILOT_USERNAME / CON_PILOT_PASSWORD."""
    expected_user = os.environ.get("CON_PILOT_USERNAME", "admin")
    expected_pass = os.environ.get("CON_PILOT_PASSWORD", "")
    if not expected_pass:
        log.warning("CON_PILOT_PASSWORD is not set — username/password login disabled")
        return False
    return username == expected_user and password == expected_pass
