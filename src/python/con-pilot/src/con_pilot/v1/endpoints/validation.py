"""Validation endpoints."""

from fastapi import APIRouter, Depends

from con_pilot.core.models import ValidationResult
from con_pilot.core.schemas import ValidateRequest

from con_pilot.conductor import ConPilot

router = APIRouter(tags=["validation"])


def get_pilot() -> ConPilot:
    """Dependency to get the ConPilot instance."""
    from con_pilot.v1.api import get_pilot as _get_pilot

    return _get_pilot()


@router.get("/validate")
def validate_get(
    config_path: str | None = None, pilot: ConPilot = Depends(get_pilot)
) -> ValidationResult:
    """Validate conductor.json against the schema (GET)."""
    pilot = pilot or get_pilot()
    return pilot.validate(config_path=config_path)


@router.post("/validate")
def validate_post(
    body: ValidateRequest | None = None, pilot: ConPilot = Depends(get_pilot)
) -> ValidationResult:
    """Validate conductor.json against the schema (POST)."""
    pilot = pilot or get_pilot()
    config_path = body.config_path if body else None
    return pilot.validate(config_path=config_path)
