# Conduct Lectern

> A VS Code extension for configuring and managing the [Conductor](https://github.com/oliben67/copilot-conductor) home directory.

Conductor is a multi-agent orchestration framework for GitHub Copilot. **Conduct Lectern** provides a dedicated activity bar panel to browse, configure, and open your Conductor home directory directly from VS Code.

---

## Features

- **Activity bar panel** — dedicated Conductor view in the VS Code sidebar
- **Home directory picker** — select or change your Conductor home directory via the command palette or the view toolbar
- **Quick open** — open the Conductor home directory in the VS Code explorer in one click
- **Auto-refresh** — the view refreshes automatically when the home directory changes

---

## Requirements

A configured [Conductor](https://github.com/oliben67/copilot-conductor) installation. The default home directory is `~/.conductor`.

---

## Extension Settings

| Setting | Default | Description |
|---|---|---|
| `conductor.home` | `~/.conductor` | Path to the Conductor home directory. Supports `~` for the user home directory. |

---

## Commands

| Command | Description |
|---|---|
| `Conductor: Open Conductor Home` | Open the Conductor home directory in the explorer |
| `Conductor: Select Conductor Home Directory` | Pick a new Conductor home directory |

---

## Getting Started

1. Install the extension.
2. Open the **Conductor** panel in the activity bar.
3. If no home directory is configured, click **Select Home Directory** and pick your `~/.conductor` folder.
4. The view will populate with your Conductor home contents.
