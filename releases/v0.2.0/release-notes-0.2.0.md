# Conductor v0.2.0

**Release Date:** April 18, 2026

> Multi-agent orchestration framework for GitHub Copilot.

*GitHub Copilot is a trademark of GitHub, Inc.*

---

## Highlights

- **Configuration versioning** — Full version management system for conductor.yaml
- **YAML configuration** — Native YAML format with backward JSON compatibility
- **Config API endpoints** — REST API for listing, creating, diffing, and activating configuration versions
- **Automatic backup** — Active configuration is backed up automatically on startup

---

## New Features

### Configuration Version Management (ConfigStore)

New `.scores/` directory in `CONDUCTOR_HOME` for versioned configuration storage:

- **Automatic backup** — Active config saved to `.scores/conductor.{version}.yaml` on startup
- **Version index** — JSON index file tracks all stored versions with timestamps
- **In-memory cache** — All configs loaded into `dict[str, ConductorConfig]` keyed by version number
- **Diff support** — Generate unified diffs between any two stored versions

### New API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/config` | GET | No | List all stored config versions |
| `/config/{version}` | GET | No | Get a specific version's full config |
| `/config` | POST | No | Create new version (fails if exists) |
| `/config/diff` | POST | No | Diff two versions |
| `/config/{version}/diff-with-active` | GET | No | Diff version vs active config |
| `/config/{version}` | PUT | Admin | Update existing version |
| `/config/{version}/activate` | POST | Admin | Activate a stored version |
| `/config/{version}` | DELETE | Admin | Delete a stored version |

### YAML Configuration Format

- Native YAML support via `conductor.yaml`
- Backward compatibility with `conductor.json`
- YAML preferred when both exist
- Version info block with semantic versioning

Example version block:
```yaml
version:
  number: "1.0.0"
  description: "Initial configuration"
  date: "2026-04-18T10:00:00Z"
  notes: "Migration from JSON format"
```

### Cron Configuration Improvements

- Replaced `has_cron_jobs` boolean with full `cron` object
- Cron expressions validated using croniter
- Optional custom cron file path per agent

```yaml
agent:
  conductor:
    cron:
      expression: "*/15 * * * *"
      file: "custom.cron"  # optional
```

---

## Breaking Changes

- Configuration files now require a `version` block for full functionality
- `has_cron_jobs` replaced with `cron.expression` object structure

---

## Bug Fixes

- Fixed schema validation for YAML config files
- Improved error messages for invalid cron expressions

---

## Test Coverage

- 124 tests passing
- 20 new tests for ConfigStore functionality
- Full coverage for version management operations

---

## Installation

```bash
# Download and run the installer
curl -LO https://github.com/oliben67/copilot-conductor/releases/download/v0.2.0/setup-0.2.0.sh
chmod +x setup-0.2.0.sh
./setup-0.2.0.sh install

# Or upgrade existing installation
./setup-0.2.0.sh update

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
