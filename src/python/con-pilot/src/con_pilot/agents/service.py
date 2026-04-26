"""Agent listing, configuration and instruction-editing logic.

Implemented as plain module-level functions taking an explicit
:class:`AgentsContext` argument.  ``ConPilot`` builds the context and
forwards its public API to these functions; nothing in this module
references ``ConPilot`` or any other concrete class via ``self``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Callable
import yaml

from con_pilot.logger import app_logger
from con_pilot.conductor.models import Agent
from con_pilot.conductor.responses import (
    AgentDetailResponse,
    AgentInfo,
    AgentListResponse,
)

log = app_logger.bind(module=__name__)


# ── Context ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AgentsContext:
    """Explicit dependency surface for the agent service functions.

    Built by :class:`con_pilot.conductor.ConPilot` and passed as the first
    argument to every public function in this module.  Keeping it frozen
    and explicit prevents the agent logic from reaching into pilot
    internals via ``self``.
    """

    config_path: str
    system_agents_dir: str
    system_retired_dir: str
    system_logs_dir: str
    templates_dir: str
    default_model: str
    get_config: Callable[[], Any]
    reload_config: Callable[[], Any]
    project_agents_dir: Callable[[str], str]
    load_trust: Callable[[], dict[str, str]]
    load_or_generate_key: Callable[[], str]


# ── Internal helpers ──────────────────────────────────────────────────────
def expand_name(
    template: str, project: str | None = None, rank: int | None = None
) -> str:
    """
    Expand name template placeholders.

    Substitutions:
        [scope:project] → project name (or removed if no project)
        [scope]         → scope name (or removed if no project)
        [rank]          → instance number (or removed if no rank)
        Any remaining [placeholder] tokens are stripped.
    """
    name = template
    name = name.replace("[scope:project]", project or "")
    name = name.replace("[scope]", project or "")
    name = name.replace("[rank]", str(rank) if rank is not None else "")
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"-+", "-", name).strip("-")
    log.debug(
        "Expanded name template '%s' with project='%s' and rank='%s' to '%s'",
        template,
        project,
        rank,
        name,
    )
    return name


def _resolve_agent_role(ctx: AgentsContext, name: str) -> str | None:
    """Resolve an agent role by role key first, then display name."""
    cfg = ctx.get_config()
    role = name if name in cfg.agents else None
    if role is None:
        for r, a in cfg.agents.items():
            if (a.name or r).lower() == name.lower():
                role = r
                break
    return role


def split_frontmatter(content: str) -> tuple[str, str]:
    """
    Split an agent file into ``(frontmatter_block, body)``.

    ``frontmatter_block`` includes the surrounding ``---`` delimiters.
    ``body`` is everything after the closing ``---``.
    """
    if content.startswith("---"):
        end = content.index("---", 3)
        fm = content[: end + 3]
        body = content[end + 3 :]
        return fm, body
    return "", content


def sync_instructions_section(fpath: str, instructions: str | None) -> bool:
    """Sync the ``## Instructions`` section of an existing agent file.

    When ``instructions`` is a non-empty string the section is replaced with
    the new text (or created if absent).  When ``instructions`` is ``None`` or
    empty the section is left as-is so manually edited instructions survive.

    Returns ``True`` when the file was modified, ``False`` when it was already
    up to date.
    """
    content = Path(fpath).read_text()
    fm, body = split_frontmatter(content)
    stripped = re.sub(r"\n## Instructions\b.*?(?=\n## |\Z)", "", body, flags=re.DOTALL)
    if instructions:
        new_body = stripped.rstrip() + f"\n\n## Instructions\n{instructions.strip()}\n"
    else:
        return False  # nothing to update
    new_content = fm + new_body
    if new_content == content:
        return False
    Path(fpath).write_text(new_content)
    return True


def _resolve_agent_files(
    ctx: AgentsContext, role: str, project: str | None
) -> list[str]:
    """Return the list of existing .agent.md file paths for a given role."""
    scope = ctx.get_config().get_agent_dict(role).get("scope", "system")
    if scope == "project" and project:
        search_dir = ctx.project_agents_dir(project)
        prefix = f"{role}.{project}."
    else:
        search_dir = ctx.system_agents_dir
        prefix = f"{role}."

    if not os.path.isdir(search_dir):
        return []
    return sorted(
        os.path.join(search_dir, f)
        for f in os.listdir(search_dir)
        if f.startswith(prefix) and f.endswith(".agent.md")
    )


def _check_system_key(ctx: AgentsContext, role: str, key: str | None) -> None:
    """Raise ValueError if a system agent is being edited without the right key."""
    if role == "conductor":
        raise ValueError("The conductor agent cannot be updated via con-pilot.")
    scope = ctx.get_config().get_agent_dict(role).get("scope", "system")
    if scope != "project":
        expected = ctx.load_or_generate_key()
        if key != expected:
            raise ValueError(
                f"System agent '{role}' requires the correct system key. "
                "Pass it with --key."
            )


def _apply_template(template: str, name: str, model: str) -> str:
    """Adapt a template file for a new agent instance."""
    result = re.sub(r'(?m)^name: ".*?"', f'name: "{name}"', template)
    result = re.sub(r'(?m)^model: ".*?"', f'model: "{model}"', result)
    result = re.sub(r"(?m)^(You are \*\*).*?(\*\*,)", rf"\g<1>{name}\2", result)
    return result


def generate_agent_file(ctx: AgentsContext, role: str, cfg: dict, model: str) -> str:
    """Return the Markdown content for a .agent.md file."""
    name = cfg.get("name", role)

    template_path = os.path.join(ctx.templates_dir, f"{role}.agent.md")
    if os.path.exists(template_path):
        with open(template_path) as f:
            return _apply_template(f.read(), name, model)

    description = cfg.get("description", "")
    sidekick_note = (
        "\n\n## Sidekick\n"
        "You are the designated **sidekick** — always available to assist "
        "with development tasks without needing to be explicitly invoked."
        if cfg.get("sidekick", False)
        else ""
    )
    if description:
        custom = cfg.get("instructions", "")
        custom_block = f"\n\n## Instructions\n{custom.strip()}" if custom else ""
        return textwrap.dedent(f"""\
            ---
            name: "{name}"
            description: "Use when: {description}"
            model: "{model}"
            tools: [read, edit, search, execute, agent, todo, web]
            ---

            You are **{name}**, the {role} agent for this system.

            ## Role
            {description}{sidekick_note}

            ## Behavior
            - Follow the session setup defined in `$CONDUCTOR_HOME/.github/copilot-instructions.md`
            - Respect `TRUSTED_DIRECTORIES` — do not operate outside them without explicit user confirmation
            - Use the model specified in `COPILOT_DEFAULT_MODEL` unless the user overrides it
            - For destructive or irreversible actions, always ask before proceeding{custom_block}
        """)
    return textwrap.dedent(f"""\
        ---
        name: "{name}"
        model: "{model}"
        ---
    """)


# ── Public API ────────────────────────────────────────────────────────────
def list_agents(ctx: AgentsContext, project: str | None = None) -> AgentListResponse:
    """List every agent defined in ``conductor.yaml`` with its on-disk status."""
    cfg = ctx.get_config()
    system_agents: list[AgentInfo] = []
    project_agents: list[AgentInfo] = []

    trust = ctx.load_trust()
    registered_projects = [p for p in trust if p != "conductor"]

    for role, agent_cfg in cfg.agents.items():
        scope = agent_cfg.scope
        instances = agent_cfg.instances
        max_inst = instances.max if instances else None

        if scope == "system":
            fname = f"{role}.agent.md"
            fpath = os.path.join(ctx.system_agents_dir, fname)
            exists = os.path.exists(fpath)

            system_agents.append(
                AgentInfo(
                    role=role,
                    name=expand_name(agent_cfg.name or role),
                    scope="system",
                    active=agent_cfg.active,
                    file_exists=exists,
                    file_path=fpath if exists else None,
                    sidekick=agent_cfg.sidekick,
                    augmenting=agent_cfg.augmenting,
                    model=agent_cfg.model,
                    description=agent_cfg.description,
                    running=agent_cfg.running
                    if isinstance(agent_cfg, Agent)
                    else False,
                )
            )
        else:
            projects_to_check = [project] if project else registered_projects
            for proj in projects_to_check:
                if not proj:
                    continue
                p_agents_dir = ctx.project_agents_dir(proj)

                if max_inst and max_inst > 1:
                    for i in range(1, max_inst + 1):
                        fname = f"{role}.{proj}.{i}.agent.md"
                        fpath = os.path.join(p_agents_dir, fname)
                        exists = os.path.exists(fpath)
                        expanded_name = expand_name(
                            agent_cfg.name or role, project=proj, rank=i
                        )
                        project_agents.append(
                            AgentInfo(
                                role=role,
                                name=expanded_name,
                                scope="project",
                                active=agent_cfg.active,
                                file_exists=exists,
                                file_path=fpath if exists else None,
                                project=proj,
                                instance=i,
                                sidekick=agent_cfg.sidekick,
                                augmenting=agent_cfg.augmenting,
                                model=agent_cfg.model,
                                description=agent_cfg.description,
                                running=agent_cfg.running
                                if isinstance(agent_cfg, Agent)
                                else False,
                            )
                        )
                else:
                    fname = f"{role}.{proj}.agent.md"
                    fpath = os.path.join(p_agents_dir, fname)
                    exists = os.path.exists(fpath)
                    expanded_name = expand_name(agent_cfg.name or role, project=proj)
                    project_agents.append(
                        AgentInfo(
                            role=role,
                            name=expanded_name,
                            scope="project",
                            active=agent_cfg.active,
                            file_exists=exists,
                            file_path=fpath if exists else None,
                            project=proj,
                            sidekick=agent_cfg.sidekick,
                            augmenting=agent_cfg.augmenting,
                            model=agent_cfg.model,
                            description=agent_cfg.description,
                            running=agent_cfg.running
                            if isinstance(agent_cfg, Agent)
                            else False,
                        )
                    )

    return AgentListResponse(
        system_agents=system_agents,
        project_agents=project_agents,
    )


def list_agent_configs(ctx: AgentsContext) -> dict[str, AgentDetailResponse]:
    """Return runtime descriptions for every configured agent."""
    result: dict[str, AgentDetailResponse] = {}
    for role in ctx.get_config().agents:
        detail = get_agent(ctx, role)
        if detail is not None:
            result[role] = detail
    return result


def get_agent_config(ctx: AgentsContext, name: str) -> AgentDetailResponse | None:
    """Return the runtime description of a single agent."""
    role = _resolve_agent_role(ctx, name)
    if role is None:
        return None
    return get_agent(ctx, role)


def update_agent_config(
    ctx: AgentsContext, name: str, changes: dict[str, Any]
) -> AgentDetailResponse | None:
    """Update mutable fields of one agent and persist the result to disk."""
    role = _resolve_agent_role(ctx, name)
    if role is None:
        return None

    mutable_fields = {
        "name",
        "active",
        "sidekick",
        "augmenting",
        "model",
        "description",
        "instructions",
    }
    unknown = [k for k in changes if k not in mutable_fields]
    if unknown:
        raise ValueError(f"Unsupported field(s): {', '.join(sorted(unknown))}")
    if not changes:
        raise ValueError("No changes provided")

    cfg = ctx.get_config()
    agent_cfg = cfg.agents[role]
    if "name" in changes:
        new_name = changes["name"]
        if new_name is not None and str(new_name).strip() == "":
            raise ValueError("Field 'name' cannot be empty")

    for field, value in changes.items():
        setattr(agent_cfg, field, value)

    config_payload = cfg.model_dump(mode="json", by_alias=True, exclude_none=True)
    with open(ctx.config_path, "w") as f:
        if ctx.config_path.endswith((".yaml", ".yml")):
            yaml.safe_dump(config_payload, f, default_flow_style=False, sort_keys=False)
        else:
            json.dump(config_payload, f, indent=2)

    ctx.reload_config()
    return get_agent_config(ctx, role)


def get_agent(ctx: AgentsContext, name: str) -> AgentDetailResponse | None:
    """Return detailed information for a single agent."""
    cfg = ctx.get_config()
    role = _resolve_agent_role(ctx, name)
    if role is None:
        return None

    agent_cfg = cfg.agents[role]
    scope = agent_cfg.scope

    if scope == "system":
        fpath = os.path.join(ctx.system_agents_dir, f"{role}.agent.md")
        file_exists = os.path.exists(fpath)
        file_path = fpath if file_exists else None
    else:
        file_exists = False
        file_path = None

    tasks = cfg.get_tasks_for_agent(role)
    permissions = agent_cfg.get_permissions().to_list()

    return AgentDetailResponse(
        role=role,
        name=expand_name(agent_cfg.name or role),
        scope=scope,
        active=agent_cfg.active,
        file_exists=file_exists,
        file_path=file_path,
        sidekick=agent_cfg.sidekick,
        augmenting=agent_cfg.augmenting,
        model=agent_cfg.model,
        description=agent_cfg.description,
        running=agent_cfg.running if isinstance(agent_cfg, Agent) else False,
        permissions=permissions,
        tasks=tasks,
        cron=agent_cfg.cron,
        instances=agent_cfg.instances,
        instructions=agent_cfg.instructions,
    )


def amend_agent(
    ctx: AgentsContext,
    instructions_file: str,
    role: str,
    project: str | None = None,
    key: str | None = None,
) -> None:
    """Amend agent files for a role by merging an ``## Instructions`` section."""
    _check_system_key(ctx, role, key)
    files = _resolve_agent_files(ctx, role, project)
    if not files:
        raise FileNotFoundError(
            f"No agent files found for role='{role}'"
            + (f" project='{project}'" if project else "")
        )
    new_instructions = Path(instructions_file).read_text().strip()
    for fpath in files:
        content = Path(fpath).read_text()
        fm, body = split_frontmatter(content)
        body = re.sub(r"\n## Instructions\b.*?(?=\n## |\Z)", "", body, flags=re.DOTALL)
        body = body.rstrip() + f"\n\n## Instructions\n{new_instructions}\n"
        Path(fpath).write_text(fm + body)
        log.info("Amended: %s", fpath)


def replace_agent(
    ctx: AgentsContext,
    instructions_file: str,
    role: str,
    project: str | None = None,
    key: str | None = None,
) -> None:
    """Replace the body of agent files for a role while preserving frontmatter."""
    _check_system_key(ctx, role, key)
    files = _resolve_agent_files(ctx, role, project)
    if not files:
        raise FileNotFoundError(
            f"No agent files found for role='{role}'"
            + (f" project='{project}'" if project else "")
        )
    new_body = "\n" + Path(instructions_file).read_text().strip() + "\n"
    for fpath in files:
        content = Path(fpath).read_text()
        fm, _ = split_frontmatter(content)
        Path(fpath).write_text(fm + new_body)
        log.info("Replaced: %s", fpath)


def reset_agent(
    ctx: AgentsContext,
    role: str,
    project: str | None = None,
    key: str | None = None,
) -> None:
    """Reset agent files for a role to their template-derived content."""
    _check_system_key(ctx, role, key)
    files = _resolve_agent_files(ctx, role, project)
    if not files:
        raise FileNotFoundError(
            f"No agent files found for role='{role}'"
            + (f" project='{project}'" if project else "")
        )
    role_cfg = ctx.get_config().get_agent_dict(role)
    name_tmpl = role_cfg.get("name", role)
    max_inst = role_cfg.get("instances", {}).get("max")

    for fpath in files:
        fname = os.path.basename(fpath)
        rank: int | None = None
        if max_inst:
            m = re.search(r"\.(\d+)\.agent\.md$", fname)
            rank = int(m.group(1)) if m else None
        expanded = expand_name(name_tmpl, project=project, rank=rank)
        content = generate_agent_file(
            ctx, role, {**role_cfg, "name": expanded}, ctx.default_model
        )
        Path(fpath).write_text(content)
        log.info("Reset: %s", fpath)


def ensure_system_agents(ctx: AgentsContext) -> None:
    """Ensure conductor.agent.md and all scope=system agents exist at startup."""
    os.makedirs(ctx.system_agents_dir, exist_ok=True)
    os.makedirs(ctx.system_retired_dir, exist_ok=True)
    os.makedirs(ctx.system_logs_dir, exist_ok=True)

    agent_cfg = ctx.get_config().agent_dicts

    system_roles = ["conductor"] + [
        role
        for role, cfg in agent_cfg.items()
        if role != "conductor"
        and cfg.get("active", False)
        and cfg.get("scope", "system") != "project"
    ]
    app_logger.debug(

        "Ensuring system agents exist for roles", system_roles=system_roles
    )
    for role in system_roles:
        fname = f"{role}.agent.md"
        dest = os.path.join(ctx.system_agents_dir, fname)
        if os.path.exists(dest):
            cfg_instructions = agent_cfg.get(role, {}).get("instructions")
            if sync_instructions_section(dest, cfg_instructions):
                log.info("Updated instructions (system): %s", fname)
            continue
        retired = os.path.join(ctx.system_retired_dir, fname)
        if os.path.exists(retired):
            shutil.move(retired, dest)
            log.info("Restored (system): %s", fname)
            continue
        cfg = agent_cfg.get(role, {})
        content = generate_agent_file(
            ctx,
            role,
            {**cfg, "name": expand_name(cfg.get("name", role))},
            ctx.default_model,
        )
        with open(dest, "w") as f:
            f.write(content)
        log.info("Created (system): %s", fname)
