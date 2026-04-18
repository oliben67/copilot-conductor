"""Sync and cron endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends

if TYPE_CHECKING:
    from con_pilot.conductor import ConPilot

router = APIRouter(tags=["sync"])


def get_pilot() -> "ConPilot":
    """Dependency to get the ConPilot instance."""
    from con_pilot.v1.api import get_pilot as _get_pilot

    return _get_pilot()


@router.post("/sync")
def sync(pilot: "ConPilot" = Depends(get_pilot)) -> dict:
    """Run a full sync cycle."""
    pilot.sync()
    return {"status": "ok"}


@router.post("/cron")
def cron(pilot: "ConPilot" = Depends(get_pilot)) -> dict:
    """Dispatch cron jobs only."""
    pilot.cron()
    return {"status": "ok"}
