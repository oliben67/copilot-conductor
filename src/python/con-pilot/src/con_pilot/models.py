"""Models module - re-exports from core.models for backward compatibility.

New code should import directly from con_pilot.core.models.
"""

from con_pilot.core.models import (
    AgentConfig,
    AgentInfo,
    AgentListResponse,
    ConductorConfig,
    CronConfig,
    InstancePolicy,
    ModelsConfig,
    ValidationError,
    ValidationResult,
    VersionConfig,
)

# Re-export exception for backward compatibility
from con_pilot.exceptions import AgentNamingPatternException

__all__ = [
    "AgentConfig",
    "AgentInfo",
    "AgentListResponse",
    "AgentNamingPatternException",
    "ConductorConfig",
    "CronConfig",
    "InstancePolicy",
    "ModelsConfig",
    "ValidationError",
    "ValidationResult",
    "VersionConfig",
]
