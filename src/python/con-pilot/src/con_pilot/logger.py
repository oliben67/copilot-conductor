"""Shared logging setup for con-pilot.

This is the only module that imports ``logging`` directly.
"""

from __future__ import annotations

import logging
import os

import structlog

_CONFIGURED = False


def configure_structlog(level: int | None = None) -> None:
    """Configure structlog with stdlib-backed loggers once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    resolved_level = level or logging.INFO
    logging.basicConfig(level=resolved_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _CONFIGURED = True


def get_logger(name: str | None = None):
    """Return a shared structlog logger."""
    configure_structlog()
    return structlog.get_logger(name)


def setup_file_logging() -> None:
    """Attach a file handler used by the CLI entrypoint."""
    log_file = os.path.join(
        os.environ.get("CONDUCTOR_HOME", os.path.expanduser("~/.conductor")),
        "con-pilot.log",
    )
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    handler = logging.FileHandler(log_file)
    handler.setFormatter(
        logging.Formatter(
            "[con-pilot %(asctime)s] %(levelname)s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# Initialize the default logger for the app
configure_structlog()
app_logger = structlog.get_logger("con_pilot")
