# Copilot Session Instructions

## Default Agent

At the start of every session, Copilot must activate the **conductor agent** defined in `.github/agents/conductor.agent.md`.

- The agent's name is read from `conductor.json` under `agent.conductor.name` and exposed as `CONDUCTOR_AGENT_NAME`.
- If the `agent.conductor` section is absent, the agent name defaults to `conductor`.
- The agent's instructions are always located at the root of `$CONDUCTOR_HOME/.github/` (this file).

## Sidekick Agent

The **sidekick** is a development-focused agent that is always active and available to assist with coding and development tasks throughout the session.

- The sidekick is the non-conductor agent with `"sidekick": true` in `conductor.json` under its `agent.<role>` section.
- Its resolved name is exposed as `SIDEKICK_AGENT_NAME`.
- **If more than one agent has `"sidekick": true`**: raise a warning at the start of the session, then resolve to `agent.developer` if that section exists. If no `agent.developer` is found, fall back to the conductor.
- If no agent has `"sidekick": true`, the conductor acts as the sidekick.

## Session Setup

At the start of every session, Copilot should ensure the environment is configured according to the setup steps defined in this repository.

### Required Setup

> **Access restriction**: Only the **conductor agent** may invoke `con-pilot` (CLI or HTTP API). All other agents must delegate con-pilot operations to the conductor.
>
> **API-first**: In production (when `con-pilot serve` is running), always use the HTTP API at `$CON_PILOT_HOST:$CON_PILOT_PORT`. The CLI is only available in debug mode (`task debug`).

1. **Run the setup steps** defined in `copilot-setup-steps.yml` located at `$CONDUCTOR_HOME/.github/copilot-setup-steps.yml`
2. **Export session environment variables** by calling `GET /setup-env` — reads `conductor.json` and returns all env vars
3. **Resolve the sidekick** — handled automatically by `/setup-env`
4. **Start the agent sync watcher** via `con-pilot serve` as a background process — runs every 15 minutes (4x/hour)

### Environment Variables

- `CONDUCTOR_HOME` - Base directory for conductor configuration (`/home/scor/.conductor`)
- `TRUSTED_DIRECTORIES` - Colon-separated list of trusted directories from `conductor.json`
- `COPILOT_DEFAULT_MODEL` - Default model, read from `conductor.json` under `models.default_model`
- `CONDUCTOR_AGENT_NAME` - Active conductor agent name, from `conductor.json` `agent.conductor.name` (fallback: `conductor`)
- `SIDEKICK_AGENT_NAME` - Resolved sidekick agent name. Falls back to conductor if unresolved.
- `PROJECT_NAME` - Name of the current project, resolved by `con-pilot setup-env` (see Project Context below).
- `SYNC_AGENTS_PID` - PID of the background sync watcher process.

### Project Context

`GET /setup-env` resolves the active project using the following strategy:

1. **Registry lookup** — checks `$CONDUCTOR_HOME/.cache/projects.json` for an entry whose `directory` matches the current working directory (or a parent/child of it).
2. **Filesystem inference** — reads `pyproject.toml` (`[project].name`), `package.json` (`name`), or `.git/config` (remote URL) in the working directory.
3. **Interactive prompt** — if running in a terminal and no name could be inferred, the user is asked to supply one.
4. Once resolved, the project name and directory are saved to `$CONDUCTOR_HOME/.cache/projects.json` and `PROJECT_NAME` is exported into the session environment.

### System Agents

Agents with `"scope": "system"` in `conductor.json` are **global** and must be running at all times. The sync watcher ensures their `.agent.md` files are always present in `.github/agents/`. At session start:

- All `scope=system` agents with `instances.min >= 1` (the default) are activated by the first sync cycle.
- These agents do not carry a project suffix in their name.

### Project Agents

Agents with `"scope": "project"` are scoped to the current project. Their `.agent.md` file names and `name:` frontmatter are derived from the `name` template in `conductor.json` with `PROJECT_NAME` substituted:

- **Single instance** (no `instances.max`, or `instances.max` not set): one file `{role}.{project}.agent.md`, `name:` becomes `{base-name}-{project}`.
- **Multiple instances** (`instances.max` is set): files `{role}.{project}.1.agent.md` … `{role}.{project}.{max}.agent.md`, `name:` becomes `{base-name}-{project}-agent-{n}`.

Name templates support the following placeholders: `[scope:project]` → project name, `[rank]` → instance number.

### Agent Sync Watcher

`con-pilot serve` runs as a background daemon every 15 minutes. It:

- **Retires** any `.agent.md` in `.github/agents/` whose role is no longer defined in `conductor.json` → moves it to `.github/agents/retired/`
- **Restores** a retired agent file if its role reappears in `conductor.json` → moves it back from `retired/` to `.github/agents/`
- **Creates** a new agent file if no match exists in `retired/` — rich (with `## Role` body) if `description` is present, minimal (name + model only) otherwise
- **Never modifies** `conductor.agent.md`

Logs are written to `$CONDUCTOR_HOME/.github/scripts/sync_agents.log`.

### Agent Cron Jobs

Agents with `"has_cron_jobs": true` in `conductor.json` have a cron file at `$CONDUCTOR_HOME/.github/agents/cron/<role>.cron` (TOML format). Each `[[job]]` entry defines:

- `name` — unique identifier for the job
- `schedule` — standard cron expression (e.g. `0 9 * * *` for daily at 9am)
- `task` — natural-language description of what the agent should do

The sync watcher checks due jobs on every cycle and appends pending tasks to `$CONDUCTOR_HOME/.github/agents/cron/pending.log`. Last-run timestamps are stored in `cron/.state/`.

**At session start**, Copilot should check `pending.log` for any unprocessed tasks and invoke the appropriate agent for each one.

### Verification

After setup, verify that:
- `CONDUCTOR_HOME` is set and `conductor.json` exists at that path
- `CONDUCTOR_AGENT_NAME` is set (verify against `conductor.json` `agent.conductor.name`)
- `SIDEKICK_AGENT_NAME` is set; if multiple sidekick agents were found, a warning was issued
- `TRUSTED_DIRECTORIES` lists the expected directories
- `PROJECT_NAME` is set; if it could not be resolved, warn the user
- System agents (`scope=system`) have `.agent.md` files present in `.github/agents/`
- Project agents (`scope=project`) have `.agent.md` files present with project-suffixed names

