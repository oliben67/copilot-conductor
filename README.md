# Conductor

A multi-agent orchestration framework for GitHub Copilot.

Conductor keeps agent definitions, runtime sync, and project trust boundaries in one place. It includes:
- `con-pilot` (Python service + CLI),
- `conduct` (operational wrapper CLI),
- packaging and install tooling.

## Current Source of Truth

- Primary config file: `conductor.json`
- System agents path: `.github/system/agents/`
- Project agents path: `.github/projects/<project>/agents/`
- Trusted project map: `.github/trust.json`

## Key Features

- Agent file reconciliation (create, retire, restore)
- Project registration and retirement workflows
- Cron dispatch pipeline with pending queue/state tracking
- FastAPI service for all lifecycle operations
- Versioned config storage under `.scores/`
- Snapshot management for `.github` workspace state
- Startup proof endpoint for Copilot SDK + conductor session status

## Architecture

```text
conductor.json + trust.json
        |
        v
   con-pilot (CLI + FastAPI)
        |
        +-- sync / cron / validate
        +-- register / retire / replace / reset
        +-- config store + snapshots
        +-- CopilotAgentService startup
```

## Quick Start

```bash
# Install dependencies (dev)
cd src/python/con-pilot
uv sync --all-groups
source .venv/bin/activate

# One-off sync
con-pilot sync

# Start API service
con-pilot serve
```

## con-pilot CLI

Current commands:

- `con-pilot sync`
- `con-pilot cron`
- `con-pilot serve [-i|--interval SECONDS]`
- `con-pilot setup-env [--shell]`
- `con-pilot register NAME DIR`
- `con-pilot retire-project NAME`
- `con-pilot list-agents [-p|--project PROJECT] [--json]`
- `con-pilot validate [FILE] [--json]`
- `con-pilot replace FILE ROLE [PROJECT] [--key KEY]`
- `con-pilot reset ROLE [PROJECT] [--key KEY]`

Note: `amend` is intentionally disabled in the current code.

## API (default prefix `/api/v1`)

Health/runtime:
- `GET /health`
- `GET /version`
- `GET /startup-proof`

Core operations:
- `POST /sync`
- `POST /cron`
- `GET/POST /validate`
- `GET /setup-env`
- `POST /register`
- `POST /retire-project`
- `POST /replace`
- `POST /reset`

Agents:
- `GET /agents`
- `GET /agents/{name}`
- `GET /agents/config`
- `GET /agents/config/{name}`
- `PATCH /agents/config/{name}` (admin key required)

Config versions (`/config`):
- list/get/create/diff/activate/update/restore/delete

Snapshots (`/snapshot`):
- list/create/check/watcher controls/download/delete

## Security Model

- System key file: `$CONDUCTOR_HOME/key`
- Admin header: `X-Admin-Key`
- Install header: `X-Install-Key`

Constraints:
- Conductor agent is protected from `replace`/`reset` edits
- System-scope edits require key authorization

## Copilot SDK Startup Proof

`GET /api/v1/startup-proof` returns runtime evidence:

- SDK package version visibility
- Copilot service wiring status
- client/session startup status
- startup completion and error state

This endpoint is the canonical way to verify conductor startup with Copilot SDK.

## Repository Layout (high-level)

- `src/python/con-pilot/` — Python package, API, CLI, tests
- `src/vscode/conduct-lectern/` — VS Code extension
- `src/schemas/` — JSON schema files
- `releases/` — packaged release artifacts

## Development

```bash
cd src/python/con-pilot

# tests
pytest -q

# lint/format
uv run ruff check src tests
uv run ruff format src tests
```
