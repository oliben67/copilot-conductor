"""
server.py — Thin shim exposing a module-level ``create_app`` factory.

This exists so uvicorn can target ``con_pilot.server:create_app`` with
``--factory`` (e.g. in ``task debug``).  All real logic lives in ``ConPilot``.
"""

from con_pilot.conductor import ConPilot

def create_app():
    """Return the FastAPI app wired to a fresh ConPilot instance."""
    return ConPilot().create_app()
