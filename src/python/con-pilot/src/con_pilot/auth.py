"""
auth.py — GitHub token resolution and validation.

Provides a singleton-style token provider that resolves COPILOT_GITHUB_TOKEN,
GH_TOKEN, or GITHUB_TOKEN from environment variables. If multiple are set with
different values, the service fails immediately.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from con_pilot.exceptions import TokenConflictError, TokenNotFoundError


@dataclass(frozen=True)
class GitHubToken:
    """
    Resolved GitHub token with its source.

    Attributes:
        value: The token string.
        source: The environment variable name it came from.
    """

    value: str
    source: str

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"GitHubToken(source={self.source!r}, value=<redacted>)"


# Priority order for token resolution
TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

_cached_token: GitHubToken | None = None


def resolve_github_token(*, required: bool = True) -> GitHubToken | None:
    """
    Resolve the GitHub token from environment variables.

    Checks COPILOT_GITHUB_TOKEN, GH_TOKEN, and GITHUB_TOKEN in that order.
    If multiple are set with different values, raises TokenConflictError.

    Args:
        required: If True, raises TokenNotFoundError when no token is found.

    Returns:
        GitHubToken with value and source, or None if not required and not found.

    Raises:
        TokenConflictError: If multiple tokens are set with different values.
        TokenNotFoundError: If required=True and no token is found.
    """
    global _cached_token  # noqa: PLW0603

    if _cached_token is not None:
        return _cached_token

    # Collect all set tokens with their sources
    found: dict[str, list[str]] = {}  # value -> list of source env var names
    for var in TOKEN_ENV_VARS:
        value = os.environ.get(var)
        if value:
            found.setdefault(value, []).append(var)

    if not found:
        if required:
            raise TokenNotFoundError()
        return None

    # Check for conflicts (multiple different values)
    if len(found) > 1:
        all_sources = [src for sources in found.values() for src in sources]
        raise TokenConflictError(all_sources)

    # Single unique value — use the highest priority source
    value = next(iter(found.keys()))
    sources = found[value]
    # Pick the first source in priority order
    for var in TOKEN_ENV_VARS:
        if var in sources:
            _cached_token = GitHubToken(value=value, source=var)
            return _cached_token

    # Fallback (shouldn't reach here)
    _cached_token = GitHubToken(value=value, source=sources[0])
    return _cached_token


def get_github_token() -> GitHubToken:
    """
    Get the resolved GitHub token (required).

    Shorthand for resolve_github_token(required=True) with type narrowing.

    Raises:
        TokenConflictError: If multiple tokens are set with different values.
        TokenNotFoundError: If no token is found.
    """
    token = resolve_github_token(required=True)
    assert token is not None  # For type narrowing
    return token


def clear_token_cache() -> None:
    """Clear the cached token (useful for testing)."""
    global _cached_token  # noqa: PLW0603
    _cached_token = None
