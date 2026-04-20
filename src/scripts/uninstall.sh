#!/usr/bin/env bash
# ── Conductor Uninstaller ─────────────────────────────────────────────────────
set -euo pipefail

APP_ID="io.conductor.ConPilot"
BASHRC="$HOME/.bashrc"
DEFAULT_HOME="$HOME/.conductor"

echo "Uninstalling Conductor…"

# Stop server
if pgrep -f 'con-pilot serve' >/dev/null 2>&1; then
  pkill -f 'con-pilot serve' || true
  echo "Stopped con-pilot serve process."
fi

# Remove Flatpak
if command -v flatpak >/dev/null 2>&1 && flatpak --user info "$APP_ID" &>/dev/null; then
  flatpak --user uninstall -y "$APP_ID"
  echo "Removed Flatpak ${APP_ID}."
fi

# Clean .bashrc
if [[ -f "$BASHRC" ]]; then
  sed -i '/^export CONDUCTOR_HOME=/d' "$BASHRC"
  sed -i '/^export CON_PILOT_HOST=/d' "$BASHRC"
  sed -i '/^export CON_PILOT_PORT=/d' "$BASHRC"
  sed -i '/^# Conductor$/d'          "$BASHRC"
  echo "Removed env vars from ${BASHRC}."
fi

# Clean venvs and sandbox data
venv_dir="${XDG_DATA_HOME:-$HOME/.local/share}/con-pilot"
[[ -d "$venv_dir" ]] && rm -rf "$venv_dir" && echo "Removed ${venv_dir}."

flatpak_data="$HOME/.var/app/${APP_ID}"
[[ -d "$flatpak_data" ]] && rm -rf "$flatpak_data" && echo "Removed ${flatpak_data}."

# Remove install directory
conductor_home="${CONDUCTOR_HOME:-$DEFAULT_HOME}"
if [[ -d "$conductor_home" ]]; then
  rm -rf "$conductor_home"
  echo "Removed ${conductor_home}."
fi

echo ""
echo "Conductor uninstalled. Restart your shell or run:  source ${BASHRC}"
