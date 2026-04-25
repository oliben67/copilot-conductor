"""Pytest configuration and shared fixtures for con-pilot tests."""

import pytest

from con_pilot.security.auth import clear_token_cache


@pytest.fixture(autouse=True)
def mock_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a mock GitHub token for all tests."""
    # Clear any cached token from previous tests
    clear_token_cache()
    # Clear all token env vars, then set only COPILOT_GITHUB_TOKEN
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test-token-for-tests")


@pytest.fixture()
def no_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all GitHub token env vars for testing token-missing scenarios."""
    clear_token_cache()
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
