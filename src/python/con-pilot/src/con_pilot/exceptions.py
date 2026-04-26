class AgentNamingPatternException(Exception):
    """Custom exception for invalid agent naming patterns."""


class PermissionHandlerException(Exception):
    """Raised when there is an issue with the permission handler setup."""


class PermissionHandlerMissingException(PermissionHandlerException):
    """Raised when no permission handler is available in the SDK."""


class AgentCreationException(Exception):
    """Raised when there is an error during agent creation."""