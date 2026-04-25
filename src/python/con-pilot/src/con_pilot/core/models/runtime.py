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
        """
        Register a callback used to determine whether an agent is currently running.

        Example:
            Agent.set_running_checker(sdk.is_running)

        Note:
            Pass ``None`` to clear the registered checker.

        :param checker: callable that returns ``True`` when the agent named
            by its argument has a live SDK session, or ``None`` to disable
            the check.
        :type checker: `Callable[[str], bool] | None`
        :return: None
        :rtype: `None`
        """
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
    def compute_running(self) -> Agent:
        """
        Refresh ``running`` after Pydantic validation completes.

        :return: this instance with ``running`` set from the registered checker.
        :rtype: `Agent`
        """
        self.running = self._compute_running()
        return self

    def set_role(self, role: str) -> Self:
        """
        Set the agent role and recompute ``running`` for the new identity.

        :param role: role identifier to assign.
        :type role: `str`
        :return: this instance with the updated role and running flag.
        :rtype: `Self`
        """
        super().set_role(role)
        self.running = self._compute_running()
        return self


class Conductor(ConductorConfig):
    """Singleton runtime config whose agents are materialized as ``Agent`` models."""

    _instance: ClassVar[Conductor | None] = None

    @model_validator(mode="after")
    def upgrade_agents(self) -> Conductor:
        """
        Ensure every entry in :attr:`agents` is a runtime :class:`Agent`.

        Note:
            Existing :class:`Agent` instances are left untouched; bare
            :class:`AgentConfig` entries are re-validated as :class:`Agent`.

        :return: this instance with the ``agents`` mapping upgraded in place.
        :rtype: `Conductor`
        """
        upgraded: dict[str, AgentConfig] = {}
        for role, cfg in self.agents.items():
            if isinstance(cfg, Agent):
                upgraded[role] = cfg
            else:
                upgraded[role] = Agent.model_validate(cfg.model_dump()).set_role(role)
        self.agents = upgraded
        return self

    @classmethod
    def instance(cls, data: dict[str, Any] | None = None) -> Conductor:
        """
        Return the singleton :class:`Conductor`, creating or refreshing it.

        Example:
            conductor = Conductor.instance(config_dict)

        Note:
            On first call ``data`` is required to build the singleton; on
            subsequent calls an optional ``data`` mapping refreshes the
            existing instance's fields in place.

        :param data: configuration dictionary used to construct or refresh
            the singleton.
        :type data: `dict[str, Any] | None`
        :return: the singleton :class:`Conductor`.
        :rtype: `Conductor`
        :raises RuntimeError: when called for the first time with ``data``
            set to ``None``.
        """
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
        """
        Discard the singleton instance.

        Note:
            Primarily used by tests and reload paths.

        :return: None
        :rtype: `None`
        """
        cls._instance = None
