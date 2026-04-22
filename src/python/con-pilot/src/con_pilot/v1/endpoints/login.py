"""Login endpoint — issue JWT tokens via username/password or token pass-through."""

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, model_validator

from con_pilot.jwt_auth import check_credentials, issue_token, verify_token

log = logging.getLogger(__name__)

router = APIRouter(tags=["auth"])


class LoginRequest(BaseModel):
    """Accepted body shapes:
    - ``{"username": "...", "password": "..."}``
    - ``{"token": "..."}``  (validates an existing JWT and returns a fresh one)
    """

    username: str | None = None
    password: str | None = None
    token: str | None = None

    @model_validator(mode="after")
    def _check_fields(self) -> "LoginRequest":
        has_creds = self.username is not None and self.password is not None
        has_token = self.token is not None
        if not has_creds and not has_token:
            raise ValueError("Provide either username+password or a token")
        if has_creds and has_token:
            raise ValueError("Provide username+password OR token, not both")
        return self


class TokenResponse(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
def login(body: LoginRequest) -> TokenResponse:
    """
    Obtain a JWT access token.

    - **username + password**: validated against ``CON_PILOT_USERNAME`` /
      ``CON_PILOT_PASSWORD`` environment variables.
    - **token**: an existing valid JWT is verified and a fresh token is issued
      for the same subject.
    """
    if body.token is not None:
        try:
            claims = verify_token(body.token)
        except Exception as exc:
            log.debug("Token validation failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        subject = claims.get("sub", "unknown")
    else:
        if not check_credentials(body.username, body.password):  # type: ignore[arg-type]
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
        subject = body.username  # type: ignore[assignment]

    token, expires_in = issue_token(subject)
    return TokenResponse(token=token, expires_in=expires_in)
