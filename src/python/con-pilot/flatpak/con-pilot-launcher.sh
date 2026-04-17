#!/bin/sh
# con-pilot-launcher — entry point for the io.conductor.ConPilot Flatpak.
#
# On first run, uses uv to create a venv with Python 3.14 and installs
# con-pilot + dependencies from the bundled wheels.  Subsequent runs skip
# the bootstrap and launch con-pilot directly.

: "${CONDUCTOR_HOME:=${HOME}/.conductor}"
export CONDUCTOR_HOME

VENV="${XDG_DATA_HOME:-$HOME/.local/share}/con-pilot/venv"
CON_PILOT="$VENV/bin/con-pilot"
WHEELS="/app/share/con-pilot/wheels"

if [ ! -x "$CON_PILOT" ]; then
    echo "[con-pilot] First run — bootstrapping Python 3.14 environment…"
    /app/bin/uv venv --python 3.14 "$VENV"
    /app/bin/uv pip install \
        --python "$VENV/bin/python" \
        --no-index \
        --find-links "$WHEELS" \
        "$WHEELS"/*.whl
    echo "[con-pilot] Bootstrap complete."
fi

exec "$CON_PILOT" "$@"
