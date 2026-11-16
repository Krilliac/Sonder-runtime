"""Resolve a chat turn's sampling temperature and show it in the activity.

Order: request > session (``/temp``) > lane > preset (none sent).  See
:mod:`sonder_runtime.domain.sampling_temperature`.
"""
from __future__ import annotations

import logging

from ..application.session.temperature_settings import STORE, session_key, temp_command
from ..domain.common.errors import InvalidInput
from ..domain.sampling_temperature import (
    ResolvedTemperature,
    check_temperature,
    parse_temperature_setting,
    resolve_temperature,
)
from .observability import activity_tracker

logger = logging.getLogger(__name__)

# The code gate's repair round trip keeps the 0.2 it always had when the
# turn itself is on auto: the repair must reproduce code, not vary it.
CODE_REPAIR_TEMPERATURE = 0.2


def resolve_turn_temperature(
    *, request: object = None, session: object = None, lane: object = None,
) -> ResolvedTemperature:
    """Resolve, record ``effective temperature=... (source: ...)``, return it."""
    resolved = resolve_temperature(
        request=request, session=STORE.get(session), lane=lane,
    )
    note = resolved.describe()
    activity_tracker.set_sampling_note(note)
    logger.debug("chat turn %s", note)
    return resolved


def session_field(raw: object, session: object) -> tuple[bool, float | None]:
    """Check a chat body's ``session_temperature`` (the GUI slider).

    Returns ``(apply, value)``: absent is ``(False, None)`` and leaves the
    session alone (a typed ``/temp`` stays); a number from 0 to 2 sets it;
    ``"auto"`` is ``(True, None)`` and clears it.  Raises ``InvalidInput``
    for anything else, or when the request names no session.  Nothing is
    stored here: the caller applies it once the request is admitted.
    """
    if raw is None:
        return False, None
    value = parse_temperature_setting(raw) if isinstance(raw, str) else check_temperature(raw)
    if session_key(session) is None:  # "", "none": the store would refuse it later
        raise InvalidInput("needs a session id in the request's session field")
    return True, value


def repair_temperature(value: float | None) -> float:
    """The code-repair temperature for a turn that resolved to ``value``."""
    return CODE_REPAIR_TEMPERATURE if value is None else float(value)


__all__ = [
    "CODE_REPAIR_TEMPERATURE",
    "repair_temperature",
    "resolve_turn_temperature",
    "session_field",
    "temp_command",
]
