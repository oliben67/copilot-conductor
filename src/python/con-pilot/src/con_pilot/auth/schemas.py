"""Session-ID validation — extensible prefix enum and format checker."""

import re
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator


class SessionIdPrefix(str, Enum):
    """Known session-ID prefixes.

    To support a new client type simply add a member here; the validator
    (:func:`validate_session_id`) picks up the new value automatically.

    Example values:
        - ``SessionIdPrefix.SHELL`` → ``"shell"``
        - ``SessionIdPrefix.WEB``   → ``"web"``
    """

    SHELL = "shell"
    WEB = "web"


_SESSION_ID_RE = re.compile(r"^(?P<prefix>[^-]+)-(?P<value>\S+)$")


def validate_session_id(value: str | None) -> str | None:
    """Validate *value* as a properly formatted session ID.

    Expected format: ``<prefix>-<opaque-value>`` where *prefix* is one of the
    :class:`SessionIdPrefix` members (e.g. ``shell-abc123``, ``web-xyz789``).

    Returns the value unchanged when valid; raises :class:`ValueError` on any
    violation.  ``None`` is accepted and returned as-is (field is optional).
    """
    if value is None:
        return None

    m = _SESSION_ID_RE.match(value)
    if not m:
        raise ValueError(
            "session_id must be '<prefix>-<value>' (e.g. 'shell-abc123'), "
            f"got: {value!r}"
        )

    prefix = m.group("prefix")
    valid = {p.value for p in SessionIdPrefix}
    if prefix not in valid:
        raise ValueError(
            f"session_id prefix must be one of {sorted(valid)}, got: {prefix!r}"
        )

    return value


# Reusable annotated type for Pydantic models and FastAPI query parameters.
SessionIdField = Annotated[str | None, AfterValidator(validate_session_id)]
