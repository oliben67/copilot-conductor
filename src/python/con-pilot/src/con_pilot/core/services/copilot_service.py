"""Copilot SDK integration service.

This module provides programmatic control over GitHub Copilot agents using the
official Copilot Python SDK. It handles:

- Creating the conductor agent at startup from YAML configuration
- Providing tools for the conductor to manage system agents
- Enforcing permissions based on agent configuration
- Managing agent sessions lifecycle

The conductor is the only agent created directly by con-pilot. All other agents
are spawned by the conductor using the tools provided here.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from con_pilot.conductor import ConPilot

try:
    from copilot import CopilotClient, SubprocessConfig, define_tool
    from copilot.generated.session_events import (
        PermissionRequest,
        SessionEventType,
    )
    from copilot.session import PermissionHandler, PermissionRequestResult

    HAS_COPILOT_SDK = True
    COPILOT_SDK_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - depends on local SDK/runtime mismatch.
    CopilotClient = Any  # type: ignore[assignment]
    SubprocessConfig = Any  # type: ignore[assignment]
    define_tool = None  # type: ignore[assignment]
    PermissionRequest = object  # type: ignore[assignment]
    SessionEventType = object  # type: ignore[assignment]
    PermissionHandler = None  # type: ignore[assignment]
    PermissionRequestResult = None  # type: ignore[assignment]

    HAS_COPILOT_SDK = False
    COPILOT_SDK_IMPORT_ERROR = f"{exc.__class__.__name__}: {exc}"

from con_pilot.core.models import Agent, AgentConfig, AgentPermissions
from con_pilot.logger import app_logger

log = app_logger.bind(module=__name__, component="CopilotAgentService")


class SpawnAgentParams(BaseModel):
    """Parameters for spawning a new agent."""

    role: str = Field(
        description="The agent role key from conductor.yaml (e.g., 'support', 'git')"
    )
    project: str | None = Field(
        default=None,
        description="Project name for project-scoped agents. Required if scope=project.",
    )


class ListAgentsParams(BaseModel):
    """Parameters for listing available agents."""

    scope: str | None = Field(
        default=None,
        description="Filter by scope: 'system' or 'project'. If None, lists all.",
    )
    active_only: bool = Field(
        default=True,
        description="If True, only return active agents.",
    )


class GetAgentPermissionsParams(BaseModel):
    """Parameters for getting agent permissions."""

    role: str = Field(description="The agent role key to get permissions for.")


class CopilotAgentService:
    """Service for managing Copilot agents via the SDK.

    This service creates and manages Copilot agent sessions programmatically.
    The conductor agent is created at startup and can spawn other agents using
    the tools registered with this service.

    Attributes:
        pilot: The ConPilot instance with configuration and paths.
        client: The CopilotClient for SDK operations (None if SDK not available).
        conductor_session: The active conductor agent session.
    """

    def __init__(self, pilot: "ConPilot") -> None:
        """Initialize the Copilot agent service.

        Args:
            pilot: The ConPilot instance providing configuration and paths.
        """
        self._pilot = pilot
        self._client: CopilotClient | None = None
        self._conductor_session: Any = None
        self._active_sessions: dict[str, Any] = {}  # role -> session
        Agent.set_running_checker(self._is_agent_running)

    @property
    def is_available(self) -> bool:
        """Check if the Copilot SDK is available."""
        return HAS_COPILOT_SDK

    async def start(self) -> None:
        """Start the Copilot client and create the conductor agent.

        This method:
        1. Initializes the CopilotClient
        2. Creates the conductor agent session with proper permissions
        3. Registers tools for agent management
        4. Sends initial context about system agents

        Raises:
            RuntimeError: If the Copilot SDK is not available.
        """
        if not HAS_COPILOT_SDK:
            log.warning(
                "Copilot SDK not available. Agent management via SDK disabled. (%s)",
                COPILOT_SDK_IMPORT_ERROR,
            )
            return

        log.info("Starting Copilot agent service...")
        log.debug("Copilot service start: loading conductor configuration")

        # Get conductor configuration
        conductor_cfg = self._pilot.config.agents.get("conductor")
        if not conductor_cfg:
            log.error("Conductor agent not found in configuration")
            return
        log.debug("Conductor config loaded", conductor_name=conductor_cfg.name)

        # Initialize the client
        github_token = self._pilot.github_token
        if github_token is None:
            log.error("GitHub token not available; cannot initialize Copilot client")
            return
        config = SubprocessConfig(
            cwd=self._pilot.home,
            github_token=github_token.value,
            env={
                "CONDUCTOR_HOME": self._pilot.home,
                "COPILOT_DEFAULT_MODEL": self._pilot.default_model,
            },
        )
        log.debug("Initializing Copilot client", cwd=self._pilot.home)
        self._client = CopilotClient(config)
        await self._client.start()
        log.debug("Copilot client started")

        # Create conductor session
        log.debug("Creating conductor session via Copilot SDK")
        await self._create_conductor_session(conductor_cfg)
        self._refresh_agent_running_flags()

        # Send system agents context to conductor
        log.debug("Sending initial system-agent context to conductor")
        await self._initialize_conductor_with_agents()

        log.info("Copilot agent service started. Conductor session active.")

    async def stop(self) -> None:
        """Stop the Copilot client and clean up sessions."""
        if not self._client:
            return

        log.info("Stopping Copilot agent service...")

        # Disconnect all active sessions
        for role, session in self._active_sessions.items():
            try:
                await session.disconnect()
                log.debug("Disconnected session for %s", role)
            except Exception as e:
                log.warning("Error disconnecting session for %s: %s", role, e)

        if self._conductor_session:
            await self._conductor_session.disconnect()

        await self._client.stop()
        self._client = None
        self._conductor_session = None
        self._active_sessions.clear()
        self._refresh_agent_running_flags()

        log.info("Copilot agent service stopped.")

    async def _create_conductor_session(self, conductor_cfg: AgentConfig) -> None:
        """Create the conductor agent session with proper configuration.

        Args:
            conductor_cfg: The conductor's AgentConfig from YAML.
        """
        if not self._client:
            return

        # Build system message from conductor config
        system_message = self._build_conductor_system_message(conductor_cfg)

        # Define tools the conductor can use
        tools = self._define_conductor_tools()

        # Create permission handler based on conductor permissions
        # Conductor has all permissions by default
        permission_handler = self._create_permission_handler(
            AgentPermissions.all_permissions()
        )

        model = conductor_cfg.model or self._pilot.default_model

        try:
            self._conductor_session = await self._client.create_session(
                model=model,
                session_id=f"conductor-{conductor_cfg.name}",
                system_message={"content": system_message},
                tools=tools,
                on_permission_request=permission_handler,
                streaming=True,
            )
        except Exception as exc:
            err = str(exc)
            if "is not available" not in err:
                raise
            log.warning(
                "Configured model '%s' is unavailable; retrying with SDK default model",
                model,
            )
            self._conductor_session = await self._client.create_session(
                model=None,
                session_id=f"conductor-{conductor_cfg.name}",
                system_message={"content": system_message},
                tools=tools,
                on_permission_request=permission_handler,
                streaming=True,
            )
        log.debug("Conductor session created", session_id=f"conductor-{conductor_cfg.name}")

        log.info(
            "Conductor session created: %s (model: %s)",
            conductor_cfg.name,
            model,
        )

    def _build_conductor_system_message(self, conductor_cfg: AgentConfig) -> str:
        """Build the system message for the conductor agent.

        Args:
            conductor_cfg: The conductor's configuration.

        Returns:
            The system message string.
        """
        name = conductor_cfg.name or "conductor"
        description = conductor_cfg.description or ""

        # Get list of system agents
        system_agents = []
        for role, cfg in self._pilot.config.agents.items():
            if role != "conductor" and cfg.scope != "project":
                system_agents.append(
                    {
                        "role": role,
                        "name": cfg.name,
                        "description": cfg.description,
                        "active": cfg.active,
                        "model": cfg.model or self._pilot.default_model,
                    }
                )

        agents_yaml = "\n".join(
            f"  - {a['role']}: {a['name']} ({'active' if a['active'] else 'inactive'})"
            for a in system_agents
        )

        return f"""You are **{name}**, the conductor agent for this system.

## Identity
{description}

## Environment
- CONDUCTOR_HOME: {self._pilot.home}
- Default Model: {self._pilot.default_model}
- Trusted Directories: {", ".join(self._trusted_directories())}

## System Agents Available
{agents_yaml}

## Your Responsibilities
1. Orchestrate task execution and manage workflow
2. Spawn and coordinate system agents using the `spawn_agent` tool
3. Ensure tasks run in correct order with efficient resource allocation
4. Monitor progress, handle issues, and optimize workflow

## Tools Available
- `spawn_agent`: Create a new agent session for a specific role
- `list_agents`: List all configured agents
- `get_agent_permissions`: Get permissions for a specific agent role

## Constraints
- Always respect TRUSTED_DIRECTORIES
- For destructive actions, always confirm before proceeding
- Only spawn agents that are defined in conductor.yaml and marked as active
"""

    def _define_conductor_tools(self) -> list:
        """Define tools available to the conductor agent.

        Returns:
            List of tool definitions for the conductor session.
        """
        if not define_tool:
            return []

        pilot = self._pilot
        service = self

        @define_tool(
            name="spawn_agent",
            description="Spawn a new agent session for a specific role from conductor.yaml",
        )
        async def spawn_agent(params: SpawnAgentParams) -> str:
            """Spawn a new agent session."""
            return await service._spawn_agent(params.role, params.project)

        @define_tool(
            name="list_agents",
            description="List all configured agents from conductor.yaml",
        )
        async def list_agents(params: ListAgentsParams) -> str:
            """List available agents."""
            agents = []
            for role, cfg in pilot.config.agents.items():
                if params.scope and cfg.scope != params.scope:
                    continue
                if params.active_only and not cfg.active:
                    continue
                agents.append(
                    {
                        "role": role,
                        "name": cfg.name,
                        "scope": cfg.scope,
                        "active": cfg.active,
                        "description": (cfg.description or "")[:100],
                    }
                )
            return f"Configured agents:\n{agents}"

        @define_tool(
            name="get_agent_permissions",
            description="Get the permissions for a specific agent role",
        )
        async def get_agent_permissions(params: GetAgentPermissionsParams) -> str:
            """Get agent permissions."""
            cfg = pilot.config.agents.get(params.role)
            if not cfg:
                return f"Agent role '{params.role}' not found"
            perms = cfg.get_permissions()
            enabled = perms.to_list()
            return f"Permissions for {params.role}:\n{enabled}"

        return [spawn_agent, list_agents, get_agent_permissions]

    async def _spawn_agent(self, role: str, project: str | None = None) -> str:
        """Spawn a new agent session for a role.

        Args:
            role: The agent role key from conductor.yaml.
            project: Project name for project-scoped agents.

        Returns:
            Success or error message.
        """
        if not self._client:
            return "Copilot SDK not available"

        cfg = self._pilot.config.agents.get(role)
        if not cfg:
            return f"Agent role '{role}' not found in configuration"

        if not cfg.active:
            return f"Agent '{role}' is not active"

        if cfg.scope == "project" and not project:
            return f"Agent '{role}' requires a project name (scope=project)"

        # Check if already spawned
        session_key = f"{role}:{project}" if project else role
        if session_key in self._active_sessions:
            return f"Agent '{role}' is already running"

        # Build system message for this agent
        system_message = self._build_agent_system_message(cfg, project)

        # Get permissions
        permissions = cfg.get_permissions()
        permission_handler = self._create_permission_handler(permissions)

        model = cfg.model or self._pilot.default_model

        try:
            session = await self._client.create_session(
                model=model,
                session_id=f"{role}-{cfg.name}-{project or 'system'}",
                system_message={"content": system_message},
                on_permission_request=permission_handler,
                streaming=True,
            )

            self._active_sessions[session_key] = session
            self._refresh_agent_running_flags()
            log.info("Spawned agent: %s (model: %s)", cfg.name, model)

            return f"Successfully spawned agent '{cfg.name}' for role '{role}'"

        except Exception as e:
            log.error("Failed to spawn agent %s: %s", role, e)
            return f"Failed to spawn agent '{role}': {e}"

    def _build_agent_system_message(
        self, cfg: AgentConfig, project: str | None = None
    ) -> str:
        """Build the system message for a non-conductor agent.

        Args:
            cfg: The agent's configuration.
            project: Optional project name.

        Returns:
            The system message string.
        """
        name = cfg.name or cfg.role or "agent"
        description = cfg.description or ""
        permissions = cfg.get_permissions()

        # Build permissions section
        allowed = permissions.to_list()
        perms_text = "\n".join(f"  - {p}" for p in allowed)

        project_context = ""
        if project:
            project_context = (
                f"\n## Project Context\nYou are operating in project: {project}"
            )

        return f"""You are **{name}**, a {cfg.role} agent for this system.

## Role
{description}

## Environment
- CONDUCTOR_HOME: {self._pilot.home}
- Default Model: {self._pilot.default_model}{project_context}

## Permissions
Your allowed operations:
{perms_text}

## Constraints
- Always respect TRUSTED_DIRECTORIES
- For destructive actions, confirm before proceeding
- Report to the conductor when tasks are complete
- Do not exceed your permission scope
"""

    def _create_permission_handler(self, permissions: AgentPermissions):
        """Create a permission handler based on agent permissions.

        Args:
            permissions: The agent's permission configuration.

        Returns:
            A permission handler function for the SDK.
        """
        if not PermissionRequestResult or not PermissionRequest:
            return PermissionHandler.approve_all if PermissionHandler else None

        def on_permission_request(
            request: "PermissionRequest", invocation: dict
        ) -> "PermissionRequestResult":
            """Handle permission requests based on agent configuration."""
            kind = (
                request.kind.value
                if hasattr(request.kind, "value")
                else str(request.kind)
            )

            # Map request kinds to permission checks
            if kind == "shell":
                if not permissions.terminal_execute:  # noqa: SIM102
                    return PermissionRequestResult(kind="denied-by-rules")
                # Check for destructive commands
                cmd = getattr(request, "full_command_text", "") or ""
                if any(d in cmd for d in ["rm ", "chmod ", "chown ", "mv ", ">"]):  # noqa: SIM102
                    if not permissions.terminal_destructive:
                        if permissions.require_approval_destructive:
                            # Would need user input - for now deny
                            return PermissionRequestResult(kind="denied-by-rules")
                        return PermissionRequestResult(kind="denied-by-rules")

            elif kind == "write":
                if not (permissions.file_create or permissions.file_modify):
                    return PermissionRequestResult(kind="denied-by-rules")

                # Check trusted directories
                if permissions.trusted_directories_only:
                    file_path = getattr(request, "file_name", "") or ""
                    trusted = self._trusted_directories()
                    if not any(file_path.startswith(t) for t in trusted):
                        return PermissionRequestResult(kind="denied-by-rules")

            elif kind == "read":
                if not permissions.workspace_read:
                    return PermissionRequestResult(kind="denied-by-rules")

            return PermissionRequestResult(kind="approved")

        return on_permission_request

    async def _initialize_conductor_with_agents(self) -> None:
        """Send initial context to conductor about system agents.

        This method sends a message to the conductor with information about
        available system agents, allowing it to spawn them as needed.
        """
        if not self._conductor_session:
            return

        # Gather system agent info
        system_agents = []
        for role, cfg in self._pilot.config.agents.items():
            if role == "conductor":
                continue
            if cfg.scope == "project":
                continue
            if not cfg.active:
                continue
            system_agents.append(
                {
                    "role": role,
                    "name": cfg.name,
                    "description": (cfg.description or "")[:200],
                }
            )

        if not system_agents:
            log.info("No active system agents to initialize")
            return

        agents_info = "\n".join(
            f"- {a['role']}: {a['name']} - {a['description']}" for a in system_agents
        )

        # Send initialization message
        init_message = f"""System agents available for spawning:

{agents_info}

Use the `spawn_agent` tool to create sessions for these agents as needed.
Active system agents should be spawned at startup to ensure they are ready
for task delegation.
"""

        done = asyncio.Event()

        def on_event(event):
            if self._event_type_value(event) == "session.idle":
                done.set()

        self._conductor_session.on(on_event)
        await self._conductor_session.send(init_message)

        # Wait for acknowledgment with timeout
        try:
            await asyncio.wait_for(done.wait(), timeout=30.0)
        except TimeoutError:
            log.warning("Conductor did not respond to initialization within timeout")

    async def send_to_conductor(self, message: str) -> str | None:
        """Send a message to the conductor and wait for response.

        Args:
            message: The message to send.

        Returns:
            The conductor's response, or None if unavailable.
        """
        if not self._conductor_session:
            return None

        response_parts = []
        done = asyncio.Event()

        def on_event(event):
            event_type = self._event_type_value(event)
            if event_type == "assistant.message":
                text = self._assistant_text(event)
                if text:
                    response_parts.append(text)
            elif event_type == "session.idle":
                done.set()

        self._conductor_session.on(on_event)
        await self._conductor_session.send(message)

        try:
            await asyncio.wait_for(done.wait(), timeout=60.0)
        except TimeoutError:
            log.warning("Conductor response timed out")

        return "".join(response_parts) if response_parts else None

    def _is_agent_running(self, agent_name: str) -> bool:
        """Return True when a Copilot SDK session appears active for this agent name."""
        target = (agent_name or "").strip().lower()
        if not target:
            return False

        sessions: list[Any] = []
        if self._conductor_session is not None:
            sessions.append(self._conductor_session)
        sessions.extend(self._active_sessions.values())

        for session in sessions:
            session_id = str(getattr(session, "session_id", getattr(session, "id", "")))
            if target in session_id.lower():
                return True
        return False

    def _refresh_agent_running_flags(self) -> None:
        """Refresh running flags for runtime Agent instances in the singleton config."""
        cfg = self._pilot.config
        for role, agent_cfg in cfg.agents.items():
            if isinstance(agent_cfg, Agent):
                name = agent_cfg.name or role
                agent_cfg.running = self._is_agent_running(name)

    @staticmethod
    def _event_type_value(event: Any) -> str:
        """Return normalized event type string across SDK variants."""
        raw = getattr(event, "type", None)
        if raw is None:
            return ""
        value = getattr(raw, "value", raw)
        return str(value)

    @staticmethod
    def _assistant_text(event: Any) -> str:
        """Extract assistant message content from SDK event payload variants."""
        data = getattr(event, "data", None)
        if data is None:
            return ""

        content = getattr(data, "content", None)
        if isinstance(content, str):
            return content

        if isinstance(data, dict):
            raw = data.get("content")
            if isinstance(raw, str):
                return raw

        return ""

    def _trusted_directories(self) -> list[str]:
        """Return trusted directories derived from the ConPilot environment."""
        raw = self._pilot.env.get("TRUSTED_DIRECTORIES", "")
        return [p for p in raw.split(":") if p]
