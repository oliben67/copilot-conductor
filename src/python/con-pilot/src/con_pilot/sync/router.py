"""Sync and cron endpoints."""

from fastapi import APIRouter, Depends

from con_pilot.conductor import ConPilot

router = APIRouter(tags=["sync"])


def get_pilot() -> ConPilot:
    """Dependency to get the ConPilot instance."""
    from con_pilot.app import get_pilot as _get_pilot

    return _get_pilot()


@router.post("/sync")
def sync(pilot: ConPilot = Depends(get_pilot)) -> dict:
    """Run a full sync cycle."""
    pilot = pilot or get_pilot()
    pilot.sync()
    return {"status": "ok"}


@router.post("/cron")
def cron(pilot: ConPilot = Depends(get_pilot)) -> dict:
    """Dispatch cron jobs only."""
    pilot = pilot or get_pilot()
    pilot.cron()
    return {"status": "ok"}
