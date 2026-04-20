# con-pilot

> The synchronisation engine, CLI, and HTTP API for the [Conductor](../../README.md) AI agent system.

`con-pilot` is a Python 3.14 package that keeps your VS Code Copilot agent roster in sync with `conductor.yaml`, dispatches scheduled cron tasks, and exposes every lifecycle operation as both a CLI and a FastAPI service.

**Deployed as a Flatpak** (`io.conductor.ConPilot`) with uv-based bootstrap — the first run installs a sandboxed Python environment and all dependencies in under 100 ms.

---

## Installation

### Via setup.sh (recommended)

`con-pilot` is installed automatically by the Conductor `setup.sh` installer:

```bash
./setup-0.3.0.sh install ~/.conductor
```

This installs the Flatpak bundle, which bootstraps Python 3.14 + all dependencies on first run.

### From source (development)

```bash
cd src/python/con-pilot
uv sync --all-groups
source .venv/bin/activate
```

The `con-pilot` entry point is then available at `.venv/bin/con-pilot`.

---

## Quick start

```bash
# Bootstrap your session
eval $(con-pilot setup-env --shell)

# One-shot sync
con-pilot sync

# Run the background watcher (con-pilot serve is also started by setup-env)
con-pilot serve
```

---

## Commands

```mermaid
mindmap
  root((con-pilot))
    Sync
      sync
      cron
      serve
    Session
      setup-env
    Projects
      register
      retire-project
    Agent editing
      replace
      reset
```

| Command | Description |
|---------|-------------|
| [`sync`](#sync) | Reconcile `.agent.md` files with `conductor.yaml` |
| [`cron`](#cron) | Dispatch due cron jobs to `pending.log` |
| [`serve`](#serve) | Run the FastAPI sync service |
| [`setup-env`](#setup-env) | Print session env vars and start the watcher |
| [`register`](#register) | Register a new project |
| [`retire-project`](#retire-project) | Archive a project |
| [`replace`](#replace) | Replace the full body of agent file(s) |
| [`reset`](#reset) | Reset agent file(s) to template/default |

---

### sync

```
con-pilot sync
```

Reads `conductor.yaml`, creates missing agent files, retires removed ones, and dispatches cron jobs. Idempotent — safe to run any number of times.

```mermaid
flowchart LR
    CF(["conductor.yaml"])

    CF --> SA["system agents\n.github/agents/"]
    CF --> PA["project agents\n.github/projects/{name}/agents/"]

    SA --> C1["create missing"]
    SA --> C2["restore from retired/"]
    SA --> C3["retire unknown"]

    PA --> D1["create missing\n(numbered instances)"]
    PA --> D2["restore from retired/"]
    PA --> D3["retire unknown"]

    CF --> CR["dispatch cron"]
```

---

### cron

```
con-pilot cron
```

Checks all agents with `has_cron_jobs: true` and appends any due tasks to `pending.log`. Called automatically at the end of every `sync` cycle.

---

### serve

```
con-pilot serve [-i SECONDS]
```

Starts a FastAPI service with a background sync loop. Default interval: 900 s (15 min).

```mermaid
flowchart TD
    SV(["con-pilot serve"])
    SV -->|"every 900 s"| S["sync()"]
    SV --> H["GET /health"]
    SV --> VER["GET /version"]
    SV --> SE["GET /setup-env"]
    SV --> AG["GET /agents"]
    SV --> MS["POST /sync"]
    SV --> MC["POST /cron"]
    SV --> VAL["GET|POST /validate"]
    SV --> REG["POST /register"]
    SV --> RET["POST /retire-project"]
    SV --> REP["POST /replace"]
    SV --> RST["POST /reset"]
    SV --> CFG["GET|POST /config/*"]
    SV --> SNP["GET|POST /snapshot/*"]
    MS -->|manual trigger| S
    MC -->|manual trigger| C["cron()"]
```

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | `{"status": "ok"}` |
| `/version` | GET | Service version string |
| `/setup-env` | GET | Resolve project context and return session env vars |
| `/agents` | GET | List all agents (`?project=` to filter) |
| `/sync` | POST | Trigger a manual sync cycle |
| `/cron` | POST | Trigger a manual cron dispatch |
| `/validate` | GET, POST | Validate `conductor.yaml` against the schema |
| `/register` | POST | Register a new project (`name`, `directory`) |
| `/retire-project` | POST | Retire a project (`name`) |
| `/replace` | POST | Replace agent body (`file_content`, `role`, `project`, `key`) |
| `/reset` | POST | Reset agent to defaults (`role`, `project`, `key`) |
| `/config` | GET | List stored configuration versions |
| `/config` | POST | Save a new configuration version |
| `/config/{version}` | GET | Retrieve a specific configuration version |
| `/config/diff` | POST | Unified diff between two stored versions |
| `/config/{version}/diff-with-active` | GET | Diff a stored version against the active config |
| `/config/{version}/activate` | POST | Activate a stored version (requires `X-Admin-Key`) |
| `/config/{version}` | DELETE | Delete a stored version (requires `X-Admin-Key`) |
| `/snapshot` | GET | List `.github` directory snapshots |
| `/snapshot` | POST | Create a new snapshot |
| `/snapshot/changes` | GET | Check for file changes since last snapshot |
| `/snapshot/check-and-create` | POST | Auto-snapshot if changes detected |
| `/snapshot/watcher` | GET | Get change-watcher status |
| `/snapshot/watcher/start` | POST | Start the change watcher |
| `/snapshot/watcher/stop` | POST | Stop the change watcher |

---

### setup-env

```
con-pilot setup-env [--shell]
```

Resolves the current project, prints all session environment variables, and spawns `con-pilot serve` as a background daemon.

```bash
# Add to your shell profile or .envrc:
eval $(con-pilot setup-env --shell)
```

```mermaid
flowchart TD
    SE(["setup-env"])
    SE --> P["resolve PROJECT_NAME"]
    P --> E["export env vars"]
    E --> V1["CONDUCTOR_HOME"]
    E --> V2["TRUSTED_DIRECTORIES"]
    E --> V3["COPILOT_DEFAULT_MODEL"]
    E --> V4["CONDUCTOR_AGENT_NAME"]
    E --> V5["SIDEKICK_AGENT_NAME"]
    E --> V6["PROJECT_NAME"]
    SE --> W["spawn con-pilot serve"]
    W --> V7["SYNC_AGENTS_PID"]
```

Output:

```
CONDUCTOR_HOME=/home/user/.conductor
TRUSTED_DIRECTORIES=/home/user/.conductor:/home/user/projects/my-app
COPILOT_DEFAULT_MODEL=claude-opus-4.6
CONDUCTOR_AGENT_NAME=uppity
SIDEKICK_AGENT_NAME=code-monkey-my-app-agent-1
PROJECT_NAME=my-app
SYNC_AGENTS_PID=48291
```

---

### register

```
con-pilot register <name> <directory>
```

Adds a project to `trust.json`, creates its agent directory scaffold, and runs an initial sync so all agent files are created immediately.

```bash
con-pilot register my-app /home/user/projects/my-app
```

---

### retire-project

```
con-pilot retire-project <name>
```

Moves `.github/projects/{name}/` to `.github/retired-projects/{name}/` and removes the project from `trust.json`. Non-destructive — the directory can be restored manually.

```bash
con-pilot retire-project my-app
```

---

### replace

```
con-pilot replace <file> <role> [project] [--key KEY]
```

Replaces the entire body of matching agent files while preserving the YAML frontmatter.

```bash
con-pilot replace new-body.md reviewer my-app
```

---

### reset

```
con-pilot reset <role> [project] [--key KEY]
```

Regenerates matching agent files from their template (`.github/agents/templates/{role}.agent.md`) or from `conductor.yaml` if no template exists.

```bash
con-pilot reset developer my-app
con-pilot reset support --key $(cat $CONDUCTOR_HOME/key)
```

---

## Security

```mermaid
flowchart LR
    CMD(["replace / reset"])
    CMD --> R{"role scope?"}
    R -->|"conductor"| BLK["🚫 always blocked"]
    R -->|"system\nsupport / arbitrator"| K{"--key correct?"}
    R -->|"project\ndeveloper / reviewer"| OK["✅ no key needed"]
    K -->|yes| OK2["✅ allowed"]
    K -->|no / missing| ERR["❌ ValueError"]
```

- **Conductor** (`conductor.agent.md`) is permanently blocked from modification via `replace` or `reset`.
- **System agents** (`scope: system`) require `--key $(cat $CONDUCTOR_HOME/key)`.
- **Project agents** (`scope: project`) require no key.

The system key is a UUID auto-generated on first use and stored at `$CONDUCTOR_HOME/key`.

Config management endpoints (`/config/{version}/activate`, `DELETE /config/{version}`) require an `X-Admin-Key` header.

---

## Development

```bash
# Run the full test suite (144 tests)
python3 -m pytest tests/ -v

# With coverage
python3 -m pytest tests/ -v --cov=con_pilot --cov-report=term-missing

# Lint + format (via uv)
uv run ruff check src/ && uv run ruff format src/

# Build the Flatpak (from the repo root)
CONDUCTOR_HOME=$(pwd) task build
```

### Flatpak build

The Flatpak bundles con-pilot with a uv-based launcher. On first run it creates a sandboxed venv and installs all wheels from the bundle:

```mermaid
flowchart LR
    FB["flatpak-builder"]
    FB --> SDK["org.freedesktop.Platform 24.08"]
    FB --> UV["uv (standalone binary)"]
    FB --> WHL["pre-built wheels"]
    FB --> LAUNCH["con-pilot-launcher.sh"]
    LAUNCH -->|"first run"| BOOT["uv venv + uv pip install"]
    LAUNCH -->|"subsequent"| RUN["uv run con-pilot"]
```

For full architecture documentation, see the [Conductor README](../../README.md).
