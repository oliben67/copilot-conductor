class AgentNamingPatternException(Exception):
    """Custom exception for invalid agent naming patterns."""

    pass


class TokenConflictError(Exception):
    """Raised when multiple GitHub token env vars are set with different values."""

    def __init__(self, sources: list[str]) -> None:
        self.sources = sources
        super().__init__(
            f"Conflicting GitHub tokens: {', '.join(sources)} are set with different values. "
            "Please ensure only one is set, or all have the same value."
        )


class TokenNotFoundError(Exception):
    """Raised when no GitHub token is available."""

    def __init__(self) -> None:
        super().__init__(
            "No GitHub token found. Set one of: COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN"
        )
