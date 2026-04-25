"""Sentry observability bootstrap for con-pilot.

The :func:`init_sentry` helper is the single entry point: it reads
configuration from the environment, performs a one-time
``sentry_sdk.init`` when a DSN is provided, and is otherwise a no-op so
production builds without Sentry credentials remain unaffected.

Recognised environment variables
--------------------------------
``SENTRY_DSN``
    Sentry project DSN. When unset or empty, :func:`init_sentry` does
    nothing and returns ``False``.

``SENTRY_ENVIRONMENT``
    Logical environment tag (defaults to ``CONDUCTOR_ENV`` when set,
    else ``production``).

``SENTRY_RELEASE``
    Release identifier; defaults to the installed ``con-pilot`` package
    version.

``SENTRY_TRACES_SAMPLE_RATE``
    Float in [0, 1] for performance traces (default ``0.0``).

``SENTRY_PROFILES_SAMPLE_RATE``
    Float in [0, 1] for profiling (default ``0.0``).

``SENTRY_SEND_DEFAULT_PII``
    Truthy to opt-in to default PII collection (default off).
"""

from __future__ import annotations

# Standard library imports:
import logging
import os
from importlib.metadata import PackageNotFoundError, version

log = logging.getLogger(__name__)

_INITIALISED = False


def _as_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _as_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _resolve_release() -> str | None:
    try:
        return version("con-pilot")
    except PackageNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        return None


def init_sentry() -> bool:
    """Initialise Sentry if ``SENTRY_DSN`` is configured.

    Idempotent — repeat calls are no-ops. Returns ``True`` when Sentry
    was actually initialised on this call, ``False`` otherwise (missing
    DSN, missing dependency, or already initialised).
    """
    global _INITIALISED
    if _INITIALISED:
        return False

    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False

    try:
        # Third party imports:
        import sentry_sdk
        from sentry_sdk.integrations.asyncio import AsyncioIntegration
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        log.warning("SENTRY_DSN set but sentry-sdk is not installed")
        return False

    environment = (
        os.environ.get("SENTRY_ENVIRONMENT")
        or os.environ.get("CONDUCTOR_ENV")
        or "production"
    )

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        release=os.environ.get("SENTRY_RELEASE") or _resolve_release(),
        traces_sample_rate=_as_float("SENTRY_TRACES_SAMPLE_RATE", 0.0),
        profiles_sample_rate=_as_float("SENTRY_PROFILES_SAMPLE_RATE", 0.0),
        send_default_pii=_as_bool("SENTRY_SEND_DEFAULT_PII", False),
        integrations=[
            AsyncioIntegration(),
            FastApiIntegration(),
            StarletteIntegration(),
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
            ),
        ],
    )

    _INITIALISED = True
    log.info("Sentry initialised (environment=%s)", environment)
    return True
