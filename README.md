# Conductor

> **A multi-agent orchestration framework for [GitHub Copilot](https://github.com/features/copilot).**

Conductor manages a fleet of AI agents defined in a single `conductor.yaml` file. It automatically creates, retires, and restores `.agent.md` files, dispatches scheduled cron tasks, and exposes every lifecycle operation through both a CLI (`conduct`) and an HTTP API (`con-pilot`).

---

## Table of Contents

- [Conductor](#conductor)
  - [Table of Contents](#table-of-contents)
  - [Features](#features)
  - [Quick start](#quick-start)
  - [Architecture](#architecture)
  - [Installation](#installation)
  - [Building from source](#building-from-source)
  - [The `conduct` CLI](#the-conduct-cli)
  - [The `setup.sh` installer](#the-setupsh-installer)
  - [Overview](#overview)
  - [How it works](#how-it-works)
  - [Directory layout](#directory-layout)
  - [conductor.yaml reference](#conductoryaml-reference)
  - [trust.json](#trustjson)
  - [Agent naming templates](#agent-naming-templates)
  - [CLI commands](#cli-commands)
    - [sync](#sync)
    - [cron](#cron)
    - [serve](#serve)
    - [setup-env](#setup-env)
    - [register](#register)
    - [retire-project](#retire-project)
    - [amend](#amend)
    - [replace](#replace)
    - [reset](#reset)
  - [Agent editing \& security](#agent-editing--security)
  - [Cron jobs](#cron-jobs)
  - [Templates](#templates)
  - [Environment variables](#environment-variables)
  - [Running the tests](#running-the-tests)

---

## Features

- **Single source of truth** — all agents defined in one `conductor.yaml`
- **Automatic sync** — creates, retires, and restores `.agent.md` files every 15 minutes
- **Flatpak packaging** — con-pilot runs in a sandboxed Flatpak with Python 3.14 and uv-based bootstrap
- **Self-extracting installer** — `setup.sh` bundles everything into a single ~23 MB file
- **Install / Update / Uninstall** — full lifecycle via `setup.sh install`, `setup.sh update`, `setup.sh uninstall`
- **Admin key security** — system agents protected by a UUID key; conductor agent permanently locked
- **Cron scheduling** — TOML-based per-agent cron jobs dispatched automatically
- **Project isolation** — trust boundaries enforced via `trust.json`; agents scoped per-project
- **Versioned config** — `ConfigStore` tracks named configuration snapshots in `.scores/` with diff and rollback
- **HTTP API** — FastAPI service with `/health`, `/version`, `/sync`, `/cron`, `/setup-env`, `/agents`, `/register`, `/retire-project`, `/validate`, `/replace`, `/reset`, `/config/*`, `/snapshot/*` endpoints
- **`conduct` CLI** — user-facing operational CLI wrapping the HTTP API
- **144 tests** — unit + CLI integration tests, all isolated with `tmp_path` fixtures

---

## Quick start

```bash
# 1. Install from the self-extracting setup.sh
./setup-0.3.0.sh install ~/.conductor

# 2. Source the environment
source ~/.bashrc

# 3. Register a project
conduct register my-app /home/user/projects/my-app

# 4. Check status
conduct status
```

---

## Architecture

```mermaid
flowchart TD
    subgraph INSTALL["Deployment"]
        SH["setup.sh\nself-extracting installer"]
        FP["Flatpak bundle\nio.conductor.ConPilot"]
        SH -->|extracts| HOME["~/.conductor/"]
        SH -->|installs| FP
    end

    subgraph RUNTIME["Runtime"]
        CONDUCT["conduct CLI\n(bash)"]
        API["con-pilot serve\n(FastAPI)"]
        CONDUCT -->|"HTTP API"| API
        CONDUCT -->|"task"| TF["Taskfile.yml"]
    end

    subgraph AGENTS["Agent Files"]
        SYS[".github/agents/\nsystem agents"]
        PROJ[".github/projects/\nproject agents"]
    end

    HOME --> RUNTIME
    FP -->|"flatpak run"| API
    API -->|"sync every 15m"| AGENTS
    API -->|"reads"| CJ["conductor.yaml"]
```

---

## Installation

### From setup.sh (recommended)

The self-extracting installer bundles the Flatpak, Taskfile, agent templates, and all configuration:

```bash
# Install
./setup-0.3.0.sh install ~/.conductor

# Update (reads CONDUCTOR_HOME from env)
./setup-0.3.0.sh update

# Uninstall
./setup-0.3.0.sh uninstall
```

On install, the admin key is displayed once and then erased. Save it — it's required for modifying system agents.

### From source (development)

See the [Building from source](#building-from-source) section below for detailed instructions.

---

## Building from source

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| [Task](https://taskfile.dev/) | v3+ | Task runner — orchestrates all build steps |
| [Flatpak](https://flatpak.org/) | 1.12+ | Sandboxed application runtime |
| [flatpak-builder](https://docs.flatpak.org/) | 1.2+ | Builds Flatpak applications |
| [Python](https://python.org/) | 3.11+ | Runtime for con-pilot |
| [uv](https://github.com/astral-sh/uv) | 0.10+ | Fast Python package manager |

Install the Freedesktop runtime and SDK (required for Flatpak build):

```bash
flatpak --user remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak --user install flathub org.freedesktop.Platform//24.08
flatpak --user install flathub org.freedesktop.Sdk//24.08
```

### Setting up the development environment

```bash
# Clone the repository
git clone https://github.com/oliben67/copilot-conductor.git
cd copilot-conductor

# Create and activate the Python virtual environment
cd src/python/con-pilot
uv sync --all-groups
source .venv/bin/activate
cd ../../..

# Verify task is available
task --version
```

### Build tasks

| Task | Description |
|------|-------------|
| `task build` | Run tests → build Flatpak → package into `setup.sh` |
| `task test` | Run the full pytest suite |
| `task flatpak` | Full Flatpak pipeline: deps → build → bundle |
| `task flatpak:force` | Clean all artifacts and rebuild from scratch |
| `task flatpak:clean` | Remove build artifacts (keeps deps cache) |
| `task flatpak:clean:all` | Remove all artifacts including deps cache |
| `task build-all` | Build everything (supports `--force` and `--release` flags) |
| `task release:package` | Package into self-extracting `setup.sh` |
| `task release:package:deps-bundled` | Package with offline dependencies |

### Quick build

```bash
# Standard build (runs tests first)
CONDUCTOR_HOME=$(pwd) task build

# The installer is created at dist/setup-{version}.sh
```

### Full build with options

```bash
# Force a clean rebuild
task build-all -- --force

# Bump version and create a release package
task build-all -- --release

# Force rebuild AND create release
task build-all -- --force --release
```

### Offline / air-gapped build

For environments without internet access, build a full standalone bundle:

```bash
# Download offline dependencies (Platform, Sdk, uv)
task flatpak:deps:offline

# Build the full bundle with offline deps included
task flatpak:bundle:full

# Package into a self-extracting installer (~500+ MB)
task release:package:deps-bundled
```

The full bundle includes:
- `org.freedesktop.Platform` runtime
- `org.freedesktop.Sdk` SDK
- `uv` binary for Python bootstrapping

### Build outputs

After a successful build:

```
dist/
├── setup-{version}.sh              # Self-extracting installer (~23 MB)
├── setup-{version}-full-bundle.sh  # Full offline installer (~500+ MB)
└── io.conductor.ConPilot.flatpak   # Standalone Flatpak bundle

src/python/con-pilot/
├── flatpak/deps/                   # Downloaded wheel dependencies
├── flatpak/build-dir/              # Flatpak build directory
└── flatpak/repo/                   # Local Flatpak repository
```

### Testing

```bash
# Run all tests with verbose output
task test -- -v

# Run a specific test file
task test -- tests/test_cli_integration.py -v

# Run tests with coverage
cd python/con-pilot
python -m pytest tests/ -v --cov=con_pilot --cov-report=term-missing
```

---

## The `conduct` CLI

`conduct` is the user-facing operational CLI. It wraps Taskfile tasks and the con-pilot HTTP API.

```
conduct — Conductor v0.3.0  (con-pilot v0.3.0)

Usage:
  conduct <command> [options]

Service commands:
  start                      Start the con-pilot service
  stop                       Stop the con-pilot service
  status                     Show whether con-pilot is running
  sync                       Trigger a one-shot sync cycle
  logs [-n N] [-f]           Show service logs (last 10 lines; -f to follow)
  agents [-p PROJECT] [-j]   List all agents (-j for JSON)

Project commands:
  register <name> <dir>      Register a new project
  retire <name>              Retire a project

Admin commands (require --key):
  admin replace <file> <role> [project] --key KEY   Replace agent body with file
  admin reset   <role> [project]        --key KEY   Reset agent(s) to defaults

General:
  version                    Show version information
  help                       Show this help message
```

---

## The `setup.sh` installer

The build process (`task build`) produces a self-extracting shell script that contains:

1. A bash header with `install`, `update`, `uninstall`, `version`, and `help` commands
2. A compressed tar archive (appended after `__ARCHIVE__` marker)

The archive includes the Taskfile (stripped of build tasks), Flatpak bundle, agent templates, conductor.yaml, cron files, and the `conduct` CLI. Dev-only files (tests, .venv, .flatpak-builder) are excluded.

```mermaid
flowchart LR
    BUILD["task build"]
    BUILD --> FP["flatpak:bundle\n(con-pilot.flatpak)"]
    BUILD --> PKG["release:package"]
    PKG --> STRIP["strip Taskfile\n(remove build tasks)"]
    PKG --> TAR["tar.gz payload"]
    PKG --> HEADER["setup.sh.header\n+ substitutions"]
    HEADER --> SETUP["setup-{version}.sh\n~23 MB"]
    TAR --> SETUP
```

---

## Overview

The **Conductor** system is a multi-agent framework built on top of GitHub Copilot. Each AI agent is defined as a `.agent.md` file under `.github/` — Copilot picks these up automatically and makes the named agent available in chat.

```mermaid
flowchart TD
    CF(["📄 conductor.yaml\nsingle source of truth"])

    CF -->|"con-pilot sync"| SA
    CF -->|"con-pilot sync"| PA

    subgraph SA["🗂 .github/agents/  —  system agents"]
        A1["conductor.agent.md"]
        A2["arbitrator.agent.md"]
        A3["support.agent.md"]
    end

    subgraph PA["🗂 .github/projects/{name}/agents/  —  project agents"]
        B1["developer.myapp.1.agent.md"]
        B2["developer.myapp.2.agent.md"]
        B3["reviewer.myapp.agent.md"]
        B4["tester.myapp.1.agent.md"]
    end
```

`con-pilot` is the CLI/service that:
- **Reconciles** `.agent.md` files with `conductor.yaml` — creating, retiring, and restoring them automatically
- **Dispatches** cron jobs defined per-agent in TOML cron files
- **Manages** project registration and trust boundaries
- **Provides** a FastAPI service for continuous background sync
- **Protects** system agents behind a GUID key to prevent accidental modification

---

## How it works

```mermaid
flowchart TD
    SE(["eval \$(con-pilot setup-env --shell)"])

    SE --> R1["resolve CONDUCTOR_HOME"]
    SE --> R2["infer PROJECT_NAME\npyproject.toml / package.json / .git/config"]
    SE --> R3["export TRUSTED_DIRECTORIES\nfrom trust.json"]
    SE --> R4["export CONDUCTOR_AGENT_NAME\n+ SIDEKICK_AGENT_NAME"]
    SE --> D["spawn con-pilot serve\n(background daemon)"]

    D -->|"every 15 min"| S["con-pilot sync"]

    S --> S1["retire unknown agents"]
    S --> S2["restore retired agents"]
    S --> S3["create new agent files"]
    S --> S4["dispatch due cron jobs"]
```

---

## Directory layout

```mermaid
graph TD
    HOME(["$CONDUCTOR_HOME/"])

    HOME --> CJ["conductor.yaml\nagent definitions & models"]
    HOME --> KEY["key\nsystem GUID (auto-generated)"]
    HOME --> VER["VERSION\ninstalled version"]
    HOME --> CONDUCT["conduct\noperational CLI"]
    HOME --> GH[".github/"]

    GH --> TJ["trust.json\nregistered projects"]
    GH --> CI["copilot-instructions.md"]
    GH --> AG["agents/"]
    GH --> PR["projects/"]
    GH --> RP["retired-projects/"]

    AG --> CA["conductor.agent.md\n(never modified)"]
    AG --> SA["*.agent.md\nsystem agents"]
    AG --> RET["retired/"]
    AG --> TPL["templates/\nbase agent templates"]
    AG --> CR["cron/"]

    CR --> CF["*.cron\nTOML job schedules"]
    CR --> PL["pending.log\nqueued tasks"]
    CR --> ST[".state/\nlast-run timestamps"]

    PR --> PROJ["{project}/"]
    PROJ --> PA["agents/\n{role}.{project}.N.agent.md"]
    PROJ --> PRET["agents/retired/"]
    PROJ --> PC["cron/"]
```

---

## conductor.yaml reference

```yaml
version:
  number: "1.0.0"
  description: "Initial configuration"
  date: "2026-04-18T10:00:00+00:00"
  notes: "Optional migration notes"

models:
  default_model: claude-opus-4.6
  authorized_models:
    - gpt-4o
    - claude-opus-4.6
    - gemini-2
    # … additional model identifiers

agent:
  # ── System agent (lives in .github/agents/) ────────────────────────────────
  arbitrator:
    name: sir                         # Copilot agent name
    description: Resolves conflicts …
    active: true
    model: claude-opus-4.6
    scope: system                     # "system" | "project"
    has_cron_jobs: true               # enables cron file creation

  # ── Project agent with numbered instances ──────────────────────────────────
  developer:
    name: "code-monkey-[scope:project]-agent-[rank]"
    description: Writes production code …
    active: true
    sidekick: true                    # exported as SIDEKICK_AGENT_NAME
    model: claude-opus-4.6
    scope: project
    instances:
      min: 1
      max: 2                          # creates developer.{proj}.1 & .2

  # ── Single-instance project agent ──────────────────────────────────────────
  reviewer:
    name: "nosy-parker-[scope:project]"
    description: Reviews PRs …
    active: true
    model: claude-opus-4.6
    scope: project
```

| `name` | string | Copilot agent display name. Supports [placeholders](#agent-naming-templates). |
| `description` | string | Shown as the agent's "Use when:" hint in Copilot. |
| `active` | bool | When `false` the agent is retired and its file moved to `retired/`. |
| `scope` | `"system"` \| `"project"` | System agents go to `.github/agents/`; project agents to `.github/projects/{name}/agents/`. |
| `model` | string | LLM model identifier. |
| `sidekick` | bool | Exactly one agent should be marked `true`; exported as `SIDEKICK_AGENT_NAME`. |
| `has_cron_jobs` | bool | Creates a TOML cron file for this agent during sync. |
| `instances.max` | int | Creates `N` numbered agent files (e.g. `developer.proj.1.agent.md` … `.N.agent.md`). |

---

## trust.json

`.github/trust.json` maps project names to their root directories. Only paths listed here are considered trusted — agents are instructed not to operate outside `TRUSTED_DIRECTORIES`.

```json
{
  "conductor": "/home/user/.conductor",
  "my-app":    "/home/user/projects/my-app",
  "api":       "/home/user/projects/api"
}
```

The `conductor` entry is always enforced and cannot be removed.

---

## Agent naming templates

Agent `name` fields support placeholder tokens that are substituted at file-creation time:

| Placeholder | Substituted with |
|-------------|------------------|
| `[scope:project]` | The current project name (e.g. `my-app`) |
| `[rank]` | The instance number for multi-instance agents (e.g. `1`, `2`) |
| Any unknown `[token]` | Removed; consecutive `-` are collapsed |

**Examples:**

```
"code-monkey-[scope:project]-agent-[rank]"
  → project=my-app, rank=2  →  "code-monkey-my-app-agent-2"

"nosy-parker-[scope:project]"
  → project=api              →  "nosy-parker-api"

"sir"
  → (system, no project)     →  "sir"
```

---

## CLI commands

All commands share the same invocation prefix:

```
con-pilot <command> [options]
```

---

### sync

Reconcile `.agent.md` files against `conductor.yaml`, then dispatch due cron jobs.

```
con-pilot sync
```

```mermaid
flowchart LR
    subgraph CFG["conductor.yaml"]
        R1["arbitrator ✓"]
        R2["support ✓"]
        R3["ghost ✗ removed"]
        R4["developer  max=2"]
        R5["reviewer  single"]
    end

    subgraph SYS[".github/agents/"]
        F1["arbitrator.agent.md  → created/kept"]
        F2["support.agent.md     → created/kept"]
        F3["ghost.agent.md       → retired/"]
    end

    subgraph PROJ[".github/projects/my-app/agents/"]
        F4["developer.my-app.1.agent.md"]
        F5["developer.my-app.2.agent.md"]
        F6["reviewer.my-app.agent.md"]
    end

    R1 --> F1
    R2 --> F2
    R3 -.->|retire| F3
    R4 --> F4
    R4 --> F5
    R5 --> F6
```

- Files for **active** roles that are missing → **created** (from template if available, or generated from config)
- Files for **active** roles that previously existed in `retired/` → **restored**
- Files for roles that are no longer active or no longer in `conductor.yaml` → **moved to `retired/`**
- `conductor.agent.md` is **never modified**

---

### cron

Dispatch cron jobs only — check all agents with `has_cron_jobs: true` and queue any due tasks to `pending.log`.

```
con-pilot cron
```

---

### serve

Start the con-pilot FastAPI service. Runs a background sync loop and exposes REST endpoints.

```
con-pilot serve [-i SECONDS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-i`, `--interval` | `900` | Seconds between sync cycles. |

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Returns `{"status": "ok"}` |
| `POST` | `/sync` | Trigger a manual sync cycle |
| `POST` | `/cron` | Trigger a manual cron dispatch |
| `GET` | `/validate` | Validate conductor.yaml against schema |

---

### setup-env

Resolve project context, print all session environment variables, and start the background watcher.

```
con-pilot setup-env [--shell]
```

```bash
# Plain output (KEY=VALUE):
con-pilot setup-env

# Shell-evaluable output (export KEY="VALUE"):
eval $(con-pilot setup-env --shell)
```

**Example output:**
```
CONDUCTOR_HOME=/home/user/.conductor
TRUSTED_DIRECTORIES=/home/user/.conductor:/home/user/projects/my-app
COPILOT_DEFAULT_MODEL=claude-opus-4.6
CONDUCTOR_AGENT_NAME=uppity
SIDEKICK_AGENT_NAME=code-monkey-my-app-agent-1
PROJECT_NAME=my-app
SYNC_AGENTS_PID=48291
```

Project name resolution order:

```mermaid
flowchart TD
    A(["resolve_project\ncwd"])
    A --> B{"pyproject.toml\n[project].name?"}
    B -->|yes| Z(["PROJECT_NAME"])
    B -->|no| C{"package.json\nname?"}
    C -->|yes| Z
    C -->|no| D{".git/config\nremote URL?"}
    D -->|yes| Z
    D -->|no| E{"directory\nbasename?"}
    E -->|yes| Z
    E -->|no| F{"stdin\nis a tty?"}
    F -->|yes| G["interactive prompt"]
    F -->|no| W(["⚠ unresolved"])
    G --> Z
```

---

### register

Register a new project: add it to `trust.json`, create its directory scaffold, and run an initial sync.

```
con-pilot register <name> <directory>
```

```bash
con-pilot register my-app /home/user/projects/my-app
```

```
✔  Registered 'my-app' at /home/user/projects/my-app
✔  Created .github/projects/my-app/agents/
✔  Created .github/projects/my-app/cron/
✔  Created developer.my-app.1.agent.md
✔  Created developer.my-app.2.agent.md
✔  Created reviewer.my-app.agent.md
✔  Created tester.my-app.1.agent.md
✔  Created tester.my-app.2.agent.md
✔  Created agile.my-app.agent.md
✔  Created git.my-app.agent.md
```

After registration, the project appears in `trust.json` and all its agents are live in Copilot.

---

### retire-project

Archive a project: move its directory to `.github/retired-projects/` and remove it from `trust.json`.

```
con-pilot retire-project <name>
```

```bash
con-pilot retire-project my-app
```

```
✔  Moved .github/projects/my-app → .github/retired-projects/my-app
✔  Removed 'my-app' from trust.json
```

If the destination already exists (e.g. a previous retirement), a timestamp suffix is appended:

```
.github/retired-projects/my-app.20260412153042
```

---

### amend

Append or replace the `## Instructions` section in matching agent file(s).
All other sections (Role, Behavior, Sidekick, etc.) are preserved.

```
con-pilot amend <file> <role> [project] [--key KEY]
```

```bash
# Amend all developer instances in a project
con-pilot amend instructions.md developer my-app

# Amend a system agent (requires system key)
con-pilot amend instructions.md support --key $(cat $CONDUCTOR_HOME/key)
```

**`instructions.md`** (example):
```markdown
- Always write unit tests for every new function.
- Follow PEP 8 and project linting rules.
- Never commit directly to main.
```

**Before:**
```markdown
---
name: "code-monkey-my-app-agent-1"
model: "claude-opus-4.6"
---

## Role
Writes production code…

## Behavior
- Follow session setup…
```

**After:**
```markdown
---
name: "code-monkey-my-app-agent-1"
model: "claude-opus-4.6"
---

## Role
Writes production code…

## Behavior
- Follow session setup…

## Instructions
- Always write unit tests for every new function.
- Follow PEP 8 and project linting rules.
- Never commit directly to main.
```

Running `amend` a second time **replaces** the `## Instructions` block, not appends.
Multi-instance agents (`developer.1`, `developer.2`, …) are **all amended simultaneously**.

---

### replace

Replace the entire body of matching agent file(s) while keeping the YAML frontmatter intact.

```
con-pilot replace <file> <role> [project] [--key KEY]
```

```bash
con-pilot replace new-body.md reviewer my-app
```

The frontmatter (`name:`, `model:`, `description:`, `tools:`) is preserved exactly.
Everything after the closing `---` is replaced with the file contents.

---

### reset

Reset agent file(s) to their template-generated or config-generated defaults.
Any custom `## Instructions` or other additions are discarded.

```
con-pilot reset <role> [project] [--key KEY]
```

```bash
# Reset all developer instances in a project
con-pilot reset developer my-app

# Reset a system agent (requires system key)
con-pilot reset support --key $(cat $CONDUCTOR_HOME/key)
```

Reset resolution order:
1. Template file at `.github/agents/templates/{role}.agent.md` (preserves body, swaps name/model)
2. Generated from `conductor.yaml` description + sidekick flag + behavior block

---

## Agent editing & security

```mermaid
flowchart LR
    A(["amend / replace / reset"])

    A --> C{"role?"}
    C -->|"conductor"| BLK["🚫 ALWAYS BLOCKED"]
    C -->|"system agent\nsupport / arbitrator"| KEY{"--key provided?"}
    C -->|"project agent\ndeveloper / reviewer / tester"| OK["✅ allowed"]

    KEY -->|"correct key"| OK2["✅ allowed"]
    KEY -->|"wrong / missing"| ERR["❌ ValueError"]
```

The **system key** is a UUID stored at `$CONDUCTOR_HOME/key`. It is generated automatically the first time a system agent would be edited. To retrieve it:

```bash
cat $CONDUCTOR_HOME/key
# e.g. 3f2a1b4c-8d7e-4f5a-9c2b-1e3d5f7a9c0b
```

Commands that accept `--key`:

| Command | Project agent | System agent | Conductor |
|---------|:---:|:---:|:---:|
| `amend` | no key | `--key` required | blocked |
| `replace` | no key | `--key` required | blocked |
| `reset` | no key | `--key` required | blocked |

---

## Cron jobs

Agents with `has_cron_jobs: true` get a TOML cron file created during sync:

```
$CONDUCTOR_HOME/.github/agents/cron/{role}.cron        ← system agents
$CONDUCTOR_HOME/.github/projects/{proj}/cron/{role}.cron ← project agents
```

**Format:**

```toml
[[job]]
name     = "daily-standup"
schedule = "0 9 * * *"          # standard cron expression
task     = "Summarise yesterday's commits and open PRs. Post to .github/output/sessions/."

[[job]]
name     = "sync-agents"
schedule = "*/15 * * * *"
task     = "Reconcile .github/agents/ with conductor.yaml."
```

```mermaid
sequenceDiagram
    participant S as con-pilot serve
    participant C as cron()
    participant F as {role}.cron
    participant P as pending.log
    participant A as Copilot agent

    loop every 15 min
        S->>C: dispatch
        C->>F: read TOML jobs
        F-->>C: [[job]] entries
        C->>C: is job due?\n(croniter / 24h fallback)
        C->>P: append pending task
        C->>C: save last-run timestamp
    end
    A->>P: read at session start
    P-->>A: pending tasks
    A->>A: execute each task
```

When a job is due, `con-pilot cron` appends it to `pending.log`:

```
[2026-04-12T09:00:01+00:00] role=conductor agent=uppity job=daily-standup schedule='0 9 * * *'
  task: Summarise yesterday's commits and open PRs…
```

Copilot reads `pending.log` at session start and invokes the appropriate agent for each entry.

---

## Templates

Place a file at `.github/agents/templates/{role}.agent.md` to customise the base content for that role.
`con-pilot` will use it as the starting point when creating or resetting agent files, substituting:

- `name: "PLACEHOLDER"` → actual expanded name
- `model: "PLACEHOLDER"` → default model from `conductor.yaml`
- `You are **PLACEHOLDER**,` → actual name in the intro line

Everything else (tools, custom sections, style) is preserved verbatim.

Example: `.github/agents/templates/developer.agent.md` is used as the base for all `developer.{proj}.N.agent.md` files.

---

## Environment variables

| Variable | Set by | Description |
|----------|--------|-------------|
| `CONDUCTOR_HOME` | `setup-env` | Absolute path to the conductor home directory. |
| `TRUSTED_DIRECTORIES` | `setup-env` | Colon-separated list of trusted project directories from `trust.json`. |
| `COPILOT_DEFAULT_MODEL` | `setup-env` | Default LLM model from `conductor.yaml`. |
| `CONDUCTOR_AGENT_NAME` | `setup-env` | Name of the conductor agent (e.g. `uppity`). |
| `SIDEKICK_AGENT_NAME` | `setup-env` | Name of the sidekick agent, with project/rank expanded (e.g. `code-monkey-my-app-agent-1`). |
| `PROJECT_NAME` | `setup-env`, `register` | Name of the current project. |
| `SYNC_AGENTS_PID` | `setup-env` | PID of the background `con-pilot serve` process. |

---

## Running the tests

### Python tests (pytest)

```bash
cd src/python/con-pilot

# Run all tests
python3 -m pytest tests/ -v

# With coverage
python3 -m pytest tests/ -v --cov=con_pilot --cov-report=term-missing
```

The test suite uses isolated `tmp_path` fixtures — no real `$CONDUCTOR_HOME` files are touched. 144 tests cover every command and edge case.

### Bash tests (BATS)

The installer helper functions in `src/conductor-cli/setup.sh.functions` are covered by a [BATS](https://bats-core.readthedocs.io/) test suite:

```bash
# Install BATS (if not already available)
npm install -g bats   # or: brew install bats-core

# Run the BATS suite
bats src/conductor-cli/tests/setup.sh.functions.bats
```

---

## Trademarks

[GitHub Copilot](https://github.com/features/copilot)®, [Copilot CLI](https://githubnext.com/projects/copilot-cli), and [Copilot SDK](https://docs.github.com/en/copilot/building-copilot-extensions/building-a-copilot-agent-for-your-copilot-extension/using-copilots-llm) are trademarks of GitHub, Inc. © GitHub, Inc. All rights reserved.

This project is not affiliated with, endorsed by, or sponsored by GitHub, Inc.
