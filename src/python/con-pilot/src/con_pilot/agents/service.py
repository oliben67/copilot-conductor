"""Agent listing, configuration and instruction-editing methods.

Mixed into :class:`con_pilot.conductor.ConPilot`. All methods rely on
attributes/methods that ``ConPilot`` itself provides: configuration
(``self.config``, ``self.reload_config``, ``self.config_path``), paths
(``self.system_agents_dir``, ``self.project_agents_dir``,
``self.system_retired_dir``, ``self.system_logs_dir``,
``self.templates_dir``), trust map (``self._load_trust``) and helpers
(``self._expand_name``, ``self._load_or_generate_key``,
``self.default_model``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import textwrap
from pathlib import Path
from typing import Any

import yaml

from con_pilot.logger import app_logger
from con_pilot.conductor.models import Agent
from con_pilot.conductor.responses import (
    AgentDetailResponse,
    AgentInfo,
    AgentListResponse,
)

log = app_logger.bind(module=__name__)


class AgentsFacet:
    """Mixin grouping agent CRUD, instruction editing and template generation."""

    def list_agents(self, project: str | None = None) -> AgentListResponse:
        """
        List every agent defined in ``conductor.yaml`` with its on-disk status.

        Example:
            response = pilot.list_agents()
            for agent in response.system_agents + response.project_agents:
                print(agent.role, agent.scope, agent.file_exists)

        Note:
            Project-scoped agents are expanded across all registered projects
            from ``trust.json`` when ``project`` is omitted; multi-instance
            agents emit one entry per declared instance.

        :param project: optional project name used to filter project-scoped
            agents. When ``None``, every registered project is scanned.
        :type project: `str | None`
        :return: a response carrying ``system_agents`` and ``project_agents``
            lists with file existence, model and runtime metadata.
        :rtype: `AgentListResponse`
        """
        cfg = self.config
        system_agents: list[AgentInfo] = []
        project_agents: list[AgentInfo] = []

        # Get all registered projects from trust.json
        trust = self._load_trust()
        registered_projects = [p for p in trust if p != "conductor"]

        for role, agent_cfg in cfg.agents.items():
            scope = agent_cfg.scope
            instances = agent_cfg.instances
            max_inst = instances.max if instances else None

            if scope == "system":
                # System agent - single file in .github/system/agents/
                fname = f"{role}.agent.md"
                fpath = os.path.join(self.system_agents_dir, fname)
                exists = os.path.exists(fpath)

                system_agents.append(
                    AgentInfo(
                        role=role,
                        name=self._expand_name(agent_cfg.name or role),
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
                # Project-scoped agent - check each project
                projects_to_check = [project] if project else registered_projects
                for proj in projects_to_check:
                    if not proj:
                        continue
                    p_agents_dir = self.project_agents_dir(proj)

                    if max_inst and max_inst > 1:
                        # Multi-instance agent
                        for i in range(1, max_inst + 1):
                            fname = f"{role}.{proj}.{i}.agent.md"
                            fpath = os.path.join(p_agents_dir, fname)
                            exists = os.path.exists(fpath)
                            expanded_name = self._expand_name(
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
                        # Single-instance project agent
                        fname = f"{role}.{proj}.agent.md"
                        fpath = os.path.join(p_agents_dir, fname)
                        exists = os.path.exists(fpath)
                        expanded_name = self._expand_name(
                            agent_cfg.name or role, project=proj
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

    def _resolve_agent_role(self, name: str) -> str | None:
        """Resolve an agent role by role key first, then display name."""
        cfg = self.config
        role = name if name in cfg.agents else None
        if role is None:
            for r, a in cfg.agents.items():
                if (a.name or r).lower() == name.lower():
                    role = r
                    break
        return role

    def list_agent_configs(self) -> dict[str, AgentDetailResponse]:
        """
        Return runtime descriptions for every configured agent.

        Example:
            for role, detail in pilot.list_agent_configs().items():
                print(role, detail.active)

        :return: a mapping of role keys to their :class:`AgentDetailResponse`.
        :rtype: `dict[str, AgentDetailResponse]`
        """
        result: dict[str, AgentDetailResponse] = {}
        for role in self.config.agents:
            detail = self.get_agent(role)
            if detail is not None:
                result[role] = detail
        return result

    def get_agent_config(self, name: str) -> AgentDetailResponse | None:
        """
        Return the runtime description of a single agent.

        Example:
            detail = pilot.get_agent_config("developer")

        :param name: the role key (e.g. ``"developer"``) or the display name
            of the agent.
        :type name: `str`
        :return: the resolved agent description, or ``None`` when no matching
            role is found.
        :rtype: `AgentDetailResponse | None`
        """
        role = self._resolve_agent_role(name)
        if role is None:
            return None
        return self.get_agent(role)

    def update_agent_config(
        self, name: str, changes: dict[str, Any]
    ) -> AgentDetailResponse | None:
        """
        Update mutable fields of one agent and persist the result to disk.

        Example:
            pilot.update_agent_config("developer", {"active": False})

        Note:
            Only the fields ``name``, ``active``, ``sidekick``, ``augmenting``,
            ``model``, ``description`` and ``instructions`` may be changed.
            The configuration file is rewritten in its original format (YAML
            or JSON) and the in-memory cache is invalidated.

        :param name: the agent role key or display name.
        :type name: `str`
        :param changes: a mapping of field name to new value.
        :type changes: `dict[str, Any]`
        :return: the refreshed agent description, or ``None`` when ``name``
            does not match a configured role.
        :rtype: `AgentDetailResponse | None`
        :raises ValueError: when ``changes`` is empty, contains an unsupported
            field, or attempts to set ``name`` to an empty string.
        """
        role = self._resolve_agent_role(name)
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

        agent_cfg = self.config.agents[role]
        if "name" in changes:
            new_name = changes["name"]
            if new_name is not None and str(new_name).strip() == "":
                raise ValueError("Field 'name' cannot be empty")

        for field, value in changes.items():
            setattr(agent_cfg, field, value)

        config_payload = self.config.model_dump(
            mode="json", by_alias=True, exclude_none=True
        )
        with open(self.config_path, "w") as f:
            if self.config_path.endswith((".yaml", ".yml")):
                yaml.safe_dump(
                    config_payload, f, default_flow_style=False, sort_keys=False
                )
            else:
                json.dump(config_payload, f, indent=2)

        self.reload_config()
        return self.get_agent_config(role)

    def get_agent(self, name: str) -> AgentDetailResponse | None:
        """
        Return detailed information for a single agent.

        Example:
            detail = pilot.get_agent("developer")
            if detail is not None and detail.file_exists:
                ...

        Note:
            File-existence checks are performed only for system-scoped agents.
            Project-scoped agents are reported with ``file_exists=False`` and
            ``file_path=None``.

        :param name: agent role key (e.g. ``"developer"``) or display name.
        :type name: `str`
        :return: the agent description, or ``None`` when ``name`` does not
            match any configured role.
        :rtype: `AgentDetailResponse | None`
        """
        cfg = self.config

        role = self._resolve_agent_role(name)

        if role is None:
            return None

        agent_cfg = cfg.agents[role]
        scope = agent_cfg.scope

        # Resolve file existence for system-scoped agents only.
        if scope == "system":
            fpath = os.path.join(self.system_agents_dir, f"{role}.agent.md")
            file_exists = os.path.exists(fpath)
            file_path = fpath if file_exists else None
        else:
            file_exists = False
            file_path = None

        tasks = cfg.get_tasks_for_agent(role)
        permissions = agent_cfg.get_permissions().to_list()

        return AgentDetailResponse(
            role=role,
            name=self._expand_name(agent_cfg.name or role),
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

    # ── Agent instruction editing ───────────────────────────────────────────────

    @staticmethod
    def _split_frontmatter(content: str) -> tuple[str, str]:
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

    def _resolve_agent_files(self, role: str, project: str | None) -> list[str]:
        """
        Return the list of existing .agent.md file paths for a given role.

        For system agents (or when project is None) looks in .github/system/agents/.
        For project agents looks in .github/projects/{project}/agents/.
        """
        scope = self.config.get_agent_dict(role).get("scope", "system")
        if scope == "project" and project:
            search_dir = self.project_agents_dir(project)
            # Match role.project.agent.md and role.project.N.agent.md
            prefix = f"{role}.{project}."
        else:
            search_dir = self.system_agents_dir
            prefix = f"{role}."

        if not os.path.isdir(search_dir):
            return []
        return sorted(
            os.path.join(search_dir, f)
            for f in os.listdir(search_dir)
            if f.startswith(prefix) and f.endswith(".agent.md")
        )

    def _check_system_key(self, role: str, key: str | None) -> None:
        """
        Raise ValueError if a system agent (non-conductor) is being edited without
        the correct system key.  Conductor is always blocked.
        """
        if role == "conductor":
            raise ValueError("The conductor agent cannot be updated via con-pilot.")
        scope = self.config.get_agent_dict(role).get("scope", "system")
        if scope != "project":
            expected = self._load_or_generate_key()
            if key != expected:
                raise ValueError(
                    f"System agent '{role}' requires the correct system key. "
                    "Pass it with --key."
                )

    def amend_agent(
        self,
        instructions_file: str,
        role: str,
        project: str | None = None,
        key: str | None = None,
    ) -> None:
        """
        Amend agent files for a role by merging an ``## Instructions`` section.

        Example:
            pilot.amend_agent("new-instructions.md", role="developer")

        Note:
            Any pre-existing ``## Instructions`` block is replaced; all other
            sections of the agent file (including the YAML frontmatter) are
            preserved. System-scoped agents require ``key`` to match the
            persisted system key; the ``conductor`` role can never be amended.

        :param instructions_file: path to a markdown file whose contents form
            the new ``## Instructions`` body.
        :type instructions_file: `str`
        :param role: agent role key (e.g. ``"developer"``).
        :type role: `str`
        :param project: optional project name; required for project-scoped
            agents.
        :type project: `str | None`
        :param key: system admin key required to mutate system-scoped agents.
        :type key: `str | None`
        :return: None
        :rtype: `None`
        :raises ValueError: when targeting the ``conductor`` role or when
            ``key`` does not match the system key for a system-scoped agent.
        :raises FileNotFoundError: when no agent file matches the role/project.
        """
        self._check_system_key(role, key)
        files = self._resolve_agent_files(role, project)
        if not files:
            raise FileNotFoundError(
                f"No agent files found for role='{role}'"
                + (f" project='{project}'" if project else "")
            )
        new_instructions = Path(instructions_file).read_text().strip()
        for fpath in files:
            content = Path(fpath).read_text()
            fm, body = self._split_frontmatter(content)
            # Remove existing ## Instructions block if present
            body = re.sub(
                r"\n## Instructions\b.*?(?=\n## |\Z)", "", body, flags=re.DOTALL
            )
            body = body.rstrip() + f"\n\n## Instructions\n{new_instructions}\n"
            Path(fpath).write_text(fm + body)
            log.info("Amended: %s", fpath)

    def replace_agent(
        self,
        instructions_file: str,
        role: str,
        project: str | None = None,
        key: str | None = None,
    ) -> None:
        """
        Replace the body of agent files for a role while preserving frontmatter.

        Example:
            pilot.replace_agent("new-body.md", role="developer")

        Note:
            The YAML frontmatter is preserved; everything below it is replaced
            with the contents of ``instructions_file``. System-scoped agents
            require ``key`` to match the persisted system key.

        :param instructions_file: path to the file whose contents become the
            new agent body.
        :type instructions_file: `str`
        :param role: agent role key.
        :type role: `str`
        :param project: optional project name; required for project-scoped
            agents.
        :type project: `str | None`
        :param key: system admin key required for system-scoped agents.
        :type key: `str | None`
        :return: None
        :rtype: `None`
        :raises ValueError: when targeting the ``conductor`` role or when the
            system key does not match.
        :raises FileNotFoundError: when no matching agent file exists.
        """
        self._check_system_key(role, key)
        files = self._resolve_agent_files(role, project)
        if not files:
            raise FileNotFoundError(
                f"No agent files found for role='{role}'"
                + (f" project='{project}'" if project else "")
            )
        new_body = "\n" + Path(instructions_file).read_text().strip() + "\n"
        for fpath in files:
            content = Path(fpath).read_text()
            fm, _ = self._split_frontmatter(content)
            Path(fpath).write_text(fm + new_body)
            log.info("Replaced: %s", fpath)

    def reset_agent(
        self, role: str, project: str | None = None, key: str | None = None
    ) -> None:
        """
        Reset agent files for a role to their template-derived content.

        Example:
            pilot.reset_agent("developer", project="my-app")

        Note:
            Existing files are overwritten using the role template (or
            regenerated from ``conductor.yaml`` when no template is found).
            System-scoped agents require ``key`` to match the system key.

        :param role: agent role key.
        :type role: `str`
        :param project: optional project name; required for project-scoped
            agents.
        :type project: `str | None`
        :param key: system admin key required for system-scoped agents.
        :type key: `str | None`
        :return: None
        :rtype: `None`
        :raises ValueError: when targeting the ``conductor`` role or when the
            system key does not match.
        :raises FileNotFoundError: when no matching agent file exists.
        """
        self._check_system_key(role, key)
        files = self._resolve_agent_files(role, project)
        if not files:
            raise FileNotFoundError(
                f"No agent files found for role='{role}'"
                + (f" project='{project}'" if project else "")
            )
        role_cfg = self.config.get_agent_dict(role)
        name_tmpl = role_cfg.get("name", role)
        max_inst = role_cfg.get("instances", {}).get("max")

        for fpath in files:
            fname = os.path.basename(fpath)
            # Determine rank for numbered instances
            rank: int | None = None
            if max_inst:
                m = re.search(r"\.(\d+)\.agent\.md$", fname)
                rank = int(m.group(1)) if m else None
            expanded = self._expand_name(name_tmpl, project=project, rank=rank)
            content = self._generate_agent_file(
                role, {**role_cfg, "name": expanded}, self.default_model
            )
            Path(fpath).write_text(content)
            log.info("Reset: %s", fpath)

    def _apply_template(self, template: str, name: str, model: str) -> str:
        """
        Adapt a template file for a new agent instance.

        Replaces the ``name:`` and ``model:`` lines in the YAML frontmatter and
        updates the first ``You are **…**`` line in the body.  Everything else
        (description, tools, ## sections) is preserved verbatim.
        """
        import re as _re

        # Replace name: and model: in frontmatter
        result = _re.sub(r'(?m)^name: ".*?"', f'name: "{name}"', template)
        result = _re.sub(r'(?m)^model: ".*?"', f'model: "{model}"', result)
        # Update the intro line "You are **old-name**,"
        result = _re.sub(r"(?m)^(You are \*\*).*?(\*\*,)", rf"\g<1>{name}\2", result)
        return result

    def _generate_agent_file(self, role: str, cfg: dict, model: str) -> str:
        """Return the Markdown content for a .agent.md file.

        If a template exists at ``.github/agents/templates/{role}.agent.md``,
        it is used as the base and adapted for ``name`` and ``model``.
        Otherwise content is generated from the conductor.json description.
        """
        name = cfg.get("name", role)

        # Use template if available
        template_path = os.path.join(self.templates_dir, f"{role}.agent.md")
        if os.path.exists(template_path):
            with open(template_path) as f:
                return self._apply_template(f.read(), name, model)

        description = cfg.get("description", "")
        sidekick_note = (
            "\n\n## Sidekick\n"
            "You are the designated **sidekick** — always available to assist "
            "with development tasks without needing to be explicitly invoked."
            if cfg.get("sidekick", False)
            else ""
        )
        if description:
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
                - For destructive or irreversible actions, always ask before proceeding
            """)
        return textwrap.dedent(f"""\
            ---
            name: "{name}"
            model: "{model}"
            ---
        """)

    def _ensure_system_agents(self) -> None:
        """Ensure conductor.agent.md and all scope=system agents exist at startup."""
        os.makedirs(self.system_agents_dir, exist_ok=True)
        os.makedirs(self.system_retired_dir, exist_ok=True)
        os.makedirs(self.system_logs_dir, exist_ok=True)

        agent_cfg = self.config.agent_dicts

        # Build list: conductor first, then other active system-scoped roles
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
            dest = os.path.join(self.system_agents_dir, fname)
            if os.path.exists(dest):
                continue
            retired = os.path.join(self.system_retired_dir, fname)
            if os.path.exists(retired):
                shutil.move(retired, dest)
                log.info("Restored (system): %s", fname)
                continue
            cfg = agent_cfg.get(role, {})
            content = self._generate_agent_file(
                role,
                {**cfg, "name": self._expand_name(cfg.get("name", role))},
                self.default_model,
            )
            with open(dest, "w") as f:
                f.write(content)
            log.info("Created (system): %s", fname)
