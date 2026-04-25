"""Project management endpoints."""

import os

from fastapi import APIRouter, Depends

from con_pilot.conductor.requests import (
    RegisterRequest,
    ReplaceRequest,
    ResetRequest,
    RetireProjectRequest,
)

from con_pilot.conductor import ConPilot

router = APIRouter(tags=["projects"])


def get_pilot() -> ConPilot:
    """Dependency to get the ConPilot instance."""
    from con_pilot.app import get_pilot as _get_pilot

    return _get_pilot()


@router.get("/setup-env")
def setup_env(pilot: ConPilot = Depends(get_pilot)) -> dict:
    """Return session environment variables derived from conductor.json."""
    pilot = pilot or get_pilot()
    result = pilot.projects.resolve()
    if result:
        os.environ["PROJECT_NAME"] = result[0]
    env = dict(pilot.env)
    if result:
        env["PROJECT_NAME"] = result[0]
    return env


@router.post("/register")
def register(body: RegisterRequest, pilot: ConPilot = Depends(get_pilot)) -> dict:
    """Register a new project."""
    pilot = pilot or get_pilot()
    pilot.projects.register(body.name, body.directory)
    return {"status": "ok"}


@router.post("/retire-project")
def retire_project(
    body: RetireProjectRequest, pilot: ConPilot = Depends(get_pilot)
) -> dict:
    """Retire a project."""
    pilot = pilot or get_pilot()
    pilot.projects.retire(body.name)
    return {"status": "ok"}


@router.post("/replace")
def replace_agent(body: ReplaceRequest, pilot: ConPilot = Depends(get_pilot)) -> dict:
    """Replace agent body entirely with the content of an instructions file."""
    pilot = pilot or get_pilot()
    pilot.agents.replace(body.file, body.role, body.project, body.key)
    return {"status": "ok"}


@router.post("/reset")
def reset_agent(body: ResetRequest, pilot: ConPilot = Depends(get_pilot)) -> dict:
    """Reset agent(s) to their template / default generated content."""
    pilot = pilot or get_pilot()
    pilot.agents.reset(body.role, body.project, body.key)
    return {"status": "ok"}
