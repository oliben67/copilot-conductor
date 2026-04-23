"""Runtime models derived from static configuration models."""

from __future__ import annotations

from typing import Any, Callable, ClassVar, Self

from pydantic import Field, model_validator

from con_pilot.core.models.config import AgentConfig, ConductorConfig


class Agent(AgentConfig):
    """Runtime agent model enriched with live status from Copilot SDK sessions."""

    running: bool = Field(
        default=False,
        description="True if an SDK-backed session for this agent is currently running.",
    )

    _running_checker: ClassVar[Callable[[str], bool] | None] = None

    @classmethod
    def set_running_checker(cls, checker: Callable[[str], bool] | None) -> None:
        """Register a callback that checks whether an agent is running."""
        cls._running_checker = checker

    def _compute_running(self) -> bool:
        """Compute running status using the registered SDK checker."""
        checker = self._running_checker
        if checker is None:
            return False
        name = self.name or self.role or ""
        try:
            return bool(checker(name))
        except Exception:
            return False

    @model_validator(mode="after")
    def compute_running(self) -> "Agent":
        """Compute running after model validation."""
        self.running = self._compute_running()
        return self

    def set_role(self, role: str) -> Self:
        """Set role and recompute running state for the updated identity."""
        super().set_role(role)
        self.running = self._compute_running()
        return self


class Conductor(ConductorConfig):
    """Singleton runtime config whose agents are materialized as ``Agent`` models."""

    _instance: ClassVar["Conductor | None"] = None

    @model_validator(mode="after")
    def upgrade_agents(self) -> "Conductor":
        """Ensure all agents are runtime ``Agent`` instances."""
        upgraded: dict[str, AgentConfig] = {}
        for role, cfg in self.agents.items():
            if isinstance(cfg, Agent):
                upgraded[role] = cfg
            else:
                upgraded[role] = Agent.model_validate(cfg.model_dump()).set_role(role)
        self.agents = upgraded
        return self

    @classmethod
    def instance(cls, data: dict[str, Any] | None = None) -> "Conductor":
        """Return the singleton instance, creating/updating it from config data."""
        if cls._instance is None:
            if data is None:
                raise RuntimeError("Conductor singleton not initialized")
            cls._instance = cls(**data)
            return cls._instance

        if data is not None:
            refreshed = cls(**data)
            for field_name in cls.model_fields:
                setattr(cls._instance, field_name, getattr(refreshed, field_name))
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (used by tests/reloads)."""
        cls._instance = None
