# Conductor v__VERSION__

**Release Date:** __RELEASE_DATE__

> Multi-agent orchestration framework for GitHub Copilot.

*GitHub Copilot is a trademark of GitHub, Inc.*

---

## Highlights

- **Self-extracting installer** — `setup-__VERSION__.sh` bundles everything into a single file
- **Flatpak packaging** — `con-pilot` runs sandboxed with Python 3.14 and uv-based bootstrap
- **Full CLI** — the `conduct` command wraps all lifecycle operations
- **HTTP API** — FastAPI-powered service for programmatic access

---

## Features

### Agent Management
- Single `conductor.json` configuration file for all agents
- Automatic sync cycle creates, retires, and restores `.agent.md` files
- System agents (global scope) and project agents (project-scoped)
- Sidekick agent designation for development assistance
- Agent naming templates with `[scope:project]` and `[rank]` placeholders

### Security
- Admin key protection for system agent modifications
- Conductor agent permanently locked (cannot be modified)
- Trust boundaries enforced via `trust.json`
- Key displayed once at install, then securely erased

### CLI (`conduct`)
- `conduct start` — Start the con-pilot service
- `conduct stop` — Stop the service
- `conduct status` — Show service status
- `conduct sync` — Trigger a manual sync cycle
- `conduct logs` — Tail the sync log
- `conduct agents` — List all agents with status
- `conduct register <name> <dir>` — Register a new project
- `conduct retire <name>` — Retire a project
- `conduct admin replace/reset` — Admin operations (require `--key`)
- Command-specific `--help` for all commands

### HTTP API (`con-pilot serve`)
- `/health` — Health check endpoint
- `/version` — Version information
- `/sync` — Trigger sync cycle
- `/cron` — Execute due cron jobs
- `/setup-env` — Export session environment
- `/agents` — List all agents
- `/register` — Register a project
- `/retire-project` — Retire a project
- `/replace` — Replace agent body (admin)
- `/reset` — Reset agent to defaults (admin)

### Installation
- `./setup-__VERSION__.sh install [CONDUCTOR_HOME]` — Full installation
- `./setup-__VERSION__.sh update` — Update existing installation
- `./setup-__VERSION__.sh uninstall` — Clean removal (same effect as `uninstall.sh`)
- `uninstall.sh` — Standalone uninstaller (same effect as `setup-__VERSION__.sh uninstall`)
- Environment variables persisted in `~/.bashrc`

### Cron Scheduling
- TOML-based per-agent cron configuration
- Automatic dispatch on sync cycles
- Pending tasks logged to `cron/pending.log`

### Bash Completion
- Tab completion for all `conduct` commands and options
- Install with `source conduct.bash-completion`

---

## Installation

```bash
# Download and run the installer
curl -LO https://github.com/oliben67/copilot-conductor/releases/download/v__VERSION__/setup-__VERSION__.sh
chmod +x setup-__VERSION__.sh
./setup-__VERSION__.sh install

# Source environment and verify
source ~/.bashrc
conduct status
```

---

## Requirements

- **Linux** (tested on Ubuntu 22.04+, Fedora 38+)
- **Flatpak** — [Installation guide](https://flatpak.org/setup/)
- **curl**, **jq** — for CLI operations
- **VS Code** with GitHub Copilot extension

---

## Changes in this Release

<!-- Add specific changes for this version here -->
