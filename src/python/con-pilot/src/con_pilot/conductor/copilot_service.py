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
from collections.abc import Callable
from typing import Any

import httpx

from con_pilot.agents.service import expand_name
from con_pilot.exceptions import AgentCreationException, PermissionHandlerMissingException
from pydantic import BaseModel, Field

from con_pilot.conductor import ConPilot
from con_pilot.conductor.fs_handler import make_fs_handler

from copilot import CopilotClient, SubprocessConfig, define_tool
from copilot.generated.session_events import PermissionRequest
from copilot.session import CopilotSession, PermissionHandler, PermissionRequestResult

from con_pilot.conductor.models import Agent, AgentConfig, AgentPermissions
from con_pilot.logger import app_logger

log = app_logger.bind(module=__name__, component="CopilotAgentService")


class HttpRequestParams(BaseModel):
    """Parameters for making an HTTP request to the con-pilot API."""

    method: str = Field(
        description="HTTP method: GET, POST, PATCH, DELETE"
    )
    path: str = Field(
        description="API path, e.g. /api/v1/documents/directories"
    )
    params: dict[str, str] | None = Field(
        default=None,
        description="Query string parameters as a key-value dict",
    )
    body: str | None = Field(
        default=None,
        description="Request body as a string (for POST/PATCH)",
    )
    content_type: str | None = Field(
        default=None,
        description="Content-Type header value for POST/PATCH requests",
    )


class SpawnAgentParams(BaseModel):
    """Parameters for spawning a new agent."""

    role: str = Field(
        description="The agent role key from conductor.yaml (e.g., 'support', 'git')"
    )
    project: str | None = Field(
        default=None,
        description="Project name for project-scoped agents. Required if [scope].",
    )
    # rank: int | None = Field(
    #     default=None,
    #     description="Rank of the agent. Required if [rank].",
    # )


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

    def __init__(self, pilot: ConPilot) -> None:
        """Initialize the Copilot agent service.

        Args:
            pilot: The ConPilot instance providing configuration and paths.
        """
        self._pilot = pilot
        self._client: CopilotClient | None = None
        self.conductor_session: CopilotSession | None = None
        self.registered_sessions: dict[str, CopilotSession] = {}  # role -> session
        Agent.set_running_checker(self._is_agent_running)

    async def start(self) -> None:
        """
        Start the Copilot client and bootstrap the conductor session.

        Example:
            await service.start()

        Note:
            Initialises a :class:`CopilotClient` subprocess, opens a streaming
            conductor session with the configured permissions, registers the
            agent-management tools and primes the conductor with the current
            system-agent context.

        :return: None
        :rtype: `None`
        :raises RuntimeError: when the Copilot SDK is not available.
        """
        log.info("Starting Copilot agent service...")
        log.debug("Copilot service start: loading conductor configuration")

        # Get conductor configuration
        conductor_cfg = self._pilot.config.agents.get("conductor")
        if not conductor_cfg:
            log.error("Conductor agent not found in configuration")
            return
        log.debug("Conductor config loaded", conductor_name=conductor_cfg.name)

        # Initialize the client
        config = SubprocessConfig(
            cwd=self._pilot.home,
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

        # Spawn sessions for every active system agent immediately at startup
        log.debug("Spawning active system agent sessions")
        await self._spawn_system_agents()

        log.info("Copilot agent service started. Conductor session active.")

    async def stop(self) -> None:
        """
        Disconnect every Copilot session and stop the SDK client.

        Example:
            await service.stop()

        Note:
            Idempotent: returns early when no client is running. Errors raised
            while disconnecting individual sessions are logged but do not
            interrupt the shutdown sequence.

        :return: None
        :rtype: `None`
        """
        if not self._client:
            return

        log.info("Stopping Copilot agent service...")

        # Disconnect all active sessions
        for role, session in self.registered_sessions.items():
            try:
                await session.disconnect()
                log.debug("Disconnected session for %s", role)
            except Exception as e:  # noqa: BLE001
                log.warning("Error disconnecting session for %s: %s", role, e)

        if self.conductor_session:
            await self.conductor_session.disconnect()

        await self._client.stop()
        self._client = None
        self.conductor_session = None
        self.registered_sessions.clear()
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
            authentication_status = await self._client.get_auth_status()
            log.debug(
                "Authentication status", authentication_status=authentication_status
            )  # Proactively check auth to provide better error handling
            self.conductor_session = await self._client.create_session(
                agent=conductor_cfg.name or "conductor",
                model=model,
                session_id=f"conductor-{conductor_cfg.name}",
                system_message={"content": system_message},
                tools=tools,
                on_permission_request=permission_handler,
                working_directory=self._pilot.home,
                create_session_fs_handler=make_fs_handler(self._pilot.home),
                enable_config_discovery=False,
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
            self.conductor_session = await self._client.create_session(
                agent=conductor_cfg.name or "conductor",
                model=None,
                session_id=f"conductor-{conductor_cfg.name}",
                system_message={"content": system_message},
                tools=tools,
                on_permission_request=permission_handler,
                working_directory=self._pilot.home,
                create_session_fs_handler=make_fs_handler(self._pilot.home),
                enable_config_discovery=False,
                streaming=True,
            )
        log.debug(
            "Conductor session created", session_id=f"conductor-{conductor_cfg.name}"
        )

        log.info(
            "Conductor session created: %s (model: %s)",
            conductor_cfg.name,
            model,
        )

    def _documents_api_instructions(self) -> str:
        """Return the documents-API instructions block, using the configured host/port."""
        host, port = self._pilot._service_config()
        base = f"http://{host}:{port}/api/v1/documents"
        return f"""\
## ⚠️ File and Directory Operations — `http_request` tool ONLY
**ONLY** and **ALWAYS** use the `http_request` tool to interact with the con-pilot
documents API for any file or directory operations.
**NEVER** use shell commands, `curl`, `bash`, `cat >`, `echo >`, `tee`, `mkdir`, `touch`,
or any SDK filesystem tools (`write_file`, `append_file`, `mkdir`, `rm`, etc.)
to create, write, move, or delete files and directories.
This is a hard rule with no exceptions.

The `http_request` tool accepts: method, path, params (dict), body (string), content_type.
The API base URL is {base}. All paths below are absolute; pass them as-is in the `path` field.

**Create a directory** (do this first, before writing any file into it)
  method=POST, path={base}/directories, params={{"path": "<dir>"}}
  → Idempotent; returns HTTP 201 with {{"path": "...", "created": true/false}}.

**Write a file**
  method=POST, path={base},
  params={{"name": "<filename>", "document_type": "<mime>", "path": "<dir>", "source": "<agent>"}},
  body=<file content as string>
  → Returns {{"id": "<uuid>", "status": "pending"}}.

**Poll write status** (required after every POST or PATCH with a body)
  method=GET, path={base}/status, params={{"id": "<uuid>"}}
  → Repeat until status is "completed" or "failed".

**Update an existing file**
  method=PATCH, path={base}/<id>,
  params={{"document_type": "...", "comment": "...", "source": "..."}}, body=<new content>
  → Status resets to "pending"; poll until completed.

**Read / find files**
  method=GET, path={base}/find, params={{"path": "<dir>", "pattern": "<glob>"}}
  → Returns matching document records including file_path.

**List a directory**
  method=GET, path={base}/directories, params={{"path": "<dir>"}}
  → Returns {{path, directories: [...], files: [...]}}

Paths may be absolute or relative to CONDUCTOR_HOME ({self._pilot.home}).\
"""

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

        custom_block = ""
        if conductor_cfg.instructions:
            custom_block = f"\n\n{conductor_cfg.instructions.strip()}"

        docs_api = self._documents_api_instructions()

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

### Agent management
- `spawn_agent`: Create a new agent session for a specific role
- `list_app_agents`: List all configured agents from conductor.yaml
- `get_agent_permissions`: Get permissions for a specific agent role

{docs_api}

## Constraints
- Always respect TRUSTED_DIRECTORIES
- For destructive shell operations (rm -rf, etc.), confirm before proceeding
- Only spawn agents that are defined in conductor.yaml and marked as active{custom_block}
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
        async def spawn_agent(params: SpawnAgentParams):
            """Spawn a new agent session."""

            async def _spawn_multiple_agents(p: SpawnAgentParams, min_count: int):
                for rank in range(1, min_count + 1):
                    await service._spawn_agent(p.role, p.project, rank=rank)

            cfg = pilot.config.agents.get(params.role)
            if cfg and cfg.instances and cfg.instances.min > 0:
                await _spawn_multiple_agents(params, cfg.instances.min)
            await service._spawn_agent(params.role, params.project)

        @define_tool(
            name="list_app_agents",
            description="List all configured agents from conductor.yaml",
        )
        async def list_app_agents(params: ListAgentsParams) -> str:
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
                        "name": cfg.name or role,
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

        @define_tool(
            name="http_request",
            description=(
                "Make an HTTP request to the con-pilot API. "
                "Use this for all file/directory operations instead of bash or curl."
            ),
        )
        async def http_request(params: HttpRequestParams) -> str:
            """Make an HTTP request to the con-pilot API."""
            host, port = pilot._service_config()
            base_url = f"http://{host}:{port}"
            url = base_url.rstrip("/") + "/" + params.path.lstrip("/")
            headers: dict[str, str] = {}
            if params.content_type:
                headers["Content-Type"] = params.content_type
            body_bytes = params.body.encode() if params.body else None
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.request(
                        method=params.method.upper(),
                        url=url,
                        params=params.params,
                        content=body_bytes,
                        headers=headers,
                    )
                return (
                    f"HTTP {response.status_code}\n"
                    f"{response.text}"
                )
            except Exception as exc:  # noqa: BLE001
                return f"HTTP request failed: {exc}"

        return [spawn_agent, list_app_agents, get_agent_permissions, http_request]

    async def _spawn_agent(
        self, role: str, project: str | None = None, rank: int | None = None
    ):
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

        if rank is not None and cfg.instances and rank > (cfg.instances.max or 0):
            raise AgentCreationException(f"Max instances exceeded for '{role}': cannot exceed {cfg.instances.max}")

        agent_name = expand_name(cfg.name or role, project, rank)

        # Check if already spawned
        session_key = f"{role}:{cfg.name}"
        if session_key in self.registered_sessions:
            return f"Agent '{role}' is already running"

        # Build system message for this agent
        system_message = self._build_agent_system_message(cfg, project)

        # Get permissions
        permissions = cfg.get_permissions()
        permission_handler = self._create_permission_handler(permissions)

        model = cfg.model or self._pilot.default_model

        try:
            working_dir = self._pilot.project_dir(project) if project else self._pilot.home
            session = await self._client.create_session(
                agent=agent_name,
                model=model,
                session_id=f"{role}-{agent_name}-{project or 'system'}",
                system_message={"content": system_message},
                on_permission_request=permission_handler,
                working_directory=working_dir,
                create_session_fs_handler=make_fs_handler(working_dir),
                enable_config_discovery=False,
                streaming=True,
            )

            self.registered_sessions[session_key] = session
            self._refresh_agent_running_flags()

            log.info(
                "Successfully spawned agent",
                agent_name=agent_name,
                role=role,
                model=model,
            )

        except Exception as e:  # noqa: BLE001
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

        custom_block = ""
        if cfg.instructions:
            custom_block = f"\n\n{cfg.instructions.strip()}"

        docs_api_block = ""
        if cfg.scope == "system":
            docs_api_block = f"{self._documents_api_instructions()}\n"

        return f"""You are **{name}**, a {cfg.role} agent for this system.

## Role
{description}

## Environment
- CONDUCTOR_HOME: {self._pilot.home}
- Default Model: {self._pilot.default_model}{project_context}

## Permissions
Your allowed operations:
{perms_text}

{docs_api_block}
## Constraints
- Always respect TRUSTED_DIRECTORIES
- For destructive actions, confirm before proceeding
- Report to the conductor when tasks are complete
- Do not exceed your permission scope{custom_block}
"""

    def _create_permission_handler(
        self, permissions: AgentPermissions
    ) -> Callable[[PermissionRequest, dict], PermissionRequestResult]:
        """Create a permission handler based on agent permissions.

        Args:
            permissions: The agent's permission configuration.

        Returns:
            A permission handler function for the SDK.
        """
        if not PermissionRequestResult or not PermissionRequest:
            if not PermissionHandler:
                raise PermissionHandlerMissingException(
                    "Copilot SDK permissions API not available"
                )

            return PermissionHandler.approve_all

        def on_permission_request(
            request: PermissionRequest, invocation: dict
        ) -> PermissionRequestResult:
            """Handle permission requests based on agent configuration."""
            kind = (
                request.kind.value
                if hasattr(request.kind, "value")
                else str(request.kind)
            )

            # Map request kinds to permission checks
            if kind == "shell":
                if not permissions.terminal_execute:
                    return PermissionRequestResult(kind="denied-by-rules")
                # Check for truly destructive commands (not output redirection)
                cmd = getattr(request, "full_command_text", "") or ""
                if any(d in cmd for d in ["rm ", "chmod ", "chown ", "mv "]):  # noqa: SIM102
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

    async def _spawn_system_agents(self) -> None:
        """Directly spawn Copilot sessions for every active system agent at startup."""
        for role, cfg in self._pilot.config.agents.items():
            if role == "conductor":
                continue
            if cfg.scope == "project":
                continue
            if not cfg.active:
                log.debug("Skipping inactive system agent: %s", role)
                continue

            max_inst = cfg.instances.max if cfg.instances else None
            min_inst = cfg.instances.min if cfg.instances else 0

            count = max(min_inst, 1)
            if max_inst and max_inst > 1 and min_inst > 1:
                count = min_inst

            if count > 1:
                for rank in range(1, count + 1):
                    log.debug("Spawning system agent: %s (rank %d)", role, rank)
                    await self._spawn_agent(role, project=None, rank=rank)
            else:
                log.debug("Spawning system agent: %s", role)
                await self._spawn_agent(role, project=None)

        self._refresh_agent_running_flags()
        log.info(
            "System agents spawned: %d session(s) registered",
            len(self.registered_sessions),
        )

    async def send_to_conductor(self, message: str) -> str | None:
        """
        Send a message to the conductor session and await its response.

        Example:
            response = await service.send_to_conductor("status")

        Note:
            Listens for ``assistant.message`` events until ``session.idle``
            is observed or 60 s elapse, whichever comes first.

        :param message: prompt text to deliver to the conductor session.
        :type message: `str`
        :return: the concatenated assistant text, or ``None`` when no
            conductor session is attached or no response was captured.
        :rtype: `str | None`
        """
        if not self.conductor_session:
            return None

        response_parts = []
        done = asyncio.Event()

        def on_event(event):
            event_type = self._event_type_value(event)
            if event_type == "assistant.message":
                text = self._assistant_text(event)
                if text:
                    response_parts.append(text)
                log.info(
                    "Received assistant.message event from conductor session",
                    event_type=event_type,
                    response_parts=response_parts,
                )
            elif event_type == "session.idle":
                done.set()
            elif event_type == "session.error":
                log.error(event.data.stack)
                done.set()

        self.conductor_session.on(on_event)
        await self.conductor_session.send(message)

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
        if self.conductor_session is not None:
            sessions.append(self.conductor_session)
        sessions.extend(self.registered_sessions.values())

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
