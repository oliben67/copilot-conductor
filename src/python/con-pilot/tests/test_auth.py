"""Tests for con_pilot.auth module — GitHub token resolution."""

from __future__ import annotations

import pytest

from con_pilot.auth import (
    GitHubToken,
    clear_token_cache,
    resolve_github_token,
)
from con_pilot.exceptions import TokenConflictError, TokenNotFoundError


class TestGitHubToken:
    """Tests for the GitHubToken dataclass."""

    def test_str_returns_value(self) -> None:
        token = GitHubToken(value="secret123", source="GH_TOKEN")
        assert str(token) == "secret123"

    def test_repr_redacts_value(self) -> None:
        token = GitHubToken(value="secret123", source="GH_TOKEN")
        assert "secret123" not in repr(token)
        assert "GH_TOKEN" in repr(token)
        assert "<redacted>" in repr(token)


class TestResolveGitHubToken:
    """Tests for resolve_github_token function."""

    def test_returns_copilot_github_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """COPILOT_GITHUB_TOKEN is resolved correctly."""
        clear_token_cache()
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "copilot-token")

        token = resolve_github_token()
        assert token is not None
        assert token.value == "copilot-token"
        assert token.source == "COPILOT_GITHUB_TOKEN"

    def test_returns_gh_token_when_no_copilot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GH_TOKEN is used when COPILOT_GITHUB_TOKEN is not set."""
        clear_token_cache()
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "gh-token")

        token = resolve_github_token()
        assert token is not None
        assert token.value == "gh-token"
        assert token.source == "GH_TOKEN"

    def test_returns_github_token_as_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GITHUB_TOKEN is used when others are not set."""
        clear_token_cache()
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "github-token")

        token = resolve_github_token()
        assert token is not None
        assert token.value == "github-token"
        assert token.source == "GITHUB_TOKEN"

    def test_accepts_same_value_in_multiple_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple vars with the same value is OK."""
        clear_token_cache()
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "same-token")
        monkeypatch.setenv("GH_TOKEN", "same-token")
        monkeypatch.setenv("GITHUB_TOKEN", "same-token")

        token = resolve_github_token()
        assert token is not None
        assert token.value == "same-token"
        # Should use highest priority source
        assert token.source == "COPILOT_GITHUB_TOKEN"

    def test_raises_on_conflicting_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Different values in multiple vars raises TokenConflictError."""
        clear_token_cache()
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "token-a")
        monkeypatch.setenv("GH_TOKEN", "token-b")

        with pytest.raises(TokenConflictError) as exc_info:
            resolve_github_token()

        assert "COPILOT_GITHUB_TOKEN" in exc_info.value.sources
        assert "GH_TOKEN" in exc_info.value.sources

    def test_raises_when_required_and_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TokenNotFoundError raised when required and no token set."""
        clear_token_cache()
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        with pytest.raises(TokenNotFoundError):
            resolve_github_token(required=True)

    def test_returns_none_when_not_required_and_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Returns None when not required and no token set."""
        clear_token_cache()
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        token = resolve_github_token(required=False)
        assert token is None

    def test_caches_resolved_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Token is cached after first resolution."""
        clear_token_cache()
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "cached-token")

        token1 = resolve_github_token()
        # Change env var - should still return cached value
        monkeypatch.setenv("GH_TOKEN", "new-token")
        token2 = resolve_github_token()

        assert token1 is token2
        assert token1.value == "cached-token"


class TestConPilotTokenIntegration:
    """Integration tests for ConPilot with token validation."""

    def test_conpilot_exposes_github_token(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConPilot.github_token returns the resolved token."""
        from con_pilot import ConPilot

        clear_token_cache()
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "pilot-token")
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path))
        assert pilot.github_token is not None
        assert pilot.github_token.value == "pilot-token"

    def test_conpilot_fails_on_conflict(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConPilot init fails when tokens conflict."""
        from con_pilot import ConPilot

        clear_token_cache()
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "token-x")
        monkeypatch.setenv("GH_TOKEN", "token-y")
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        with pytest.raises(TokenConflictError):
            ConPilot(str(tmp_path))

    def test_conpilot_require_token_false(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ConPilot with require_token=False works without token."""
        from con_pilot import ConPilot

        clear_token_cache()
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path))

        pilot = ConPilot(str(tmp_path), require_token=False)
        assert pilot.github_token is None
