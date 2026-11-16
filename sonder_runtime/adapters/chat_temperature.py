"""Resolve a chat turn's sampling temperature and show it in the activity.

Order: request > session (``/temp``) > lane > preset (none sent).  See
:mod:`sonder_runtime.domain.sampling_temperature`.
"""
from __future__ import annotations

import logging

from ..application.session.temperature_settings import STORE, temp_command
from ..domain.sampling_temperature import ResolvedTemperature, resolve_temperature
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


def repair_temperature(value: float | None) -> float:
    """The code-repair temperature for a turn that resolved to ``value``."""
    return CODE_REPAIR_TEMPERATURE if value is None else float(value)


__all__ = [
    "CODE_REPAIR_TEMPERATURE",
    "repair_temperature",
    "resolve_turn_temperature",
    "temp_command",
]
