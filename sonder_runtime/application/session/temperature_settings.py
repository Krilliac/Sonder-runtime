"""Per-session sampling temperature, set with ``/temp`` or the GUI slider.

The value lives with the conversation thread (keyed by its session id), not
in a global: two threads can run at different temperatures.  It is held in
memory for the life of the process; a restart returns every thread to
``auto`` (the model preset), which is the safe default.

``None`` is never stored: clearing a thread (``/temp auto``) removes its entry.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

from ...domain.common.errors import InvalidInput
from ...domain.sampling_temperature import (
    check_temperature,
    format_temperature,
    parse_temperature_setting,
)

MAX_SESSIONS = 4096
# Session ids that mean "no thread": a one-off turn has nowhere to keep a value.
_NO_SESSION = frozenset({"", "none"})


def session_key(session: object) -> str | None:
    """The store key for ``session``, or ``None`` when there is no thread."""
    key = str(session if session is not None else "").strip()
    return None if key.lower() in _NO_SESSION else key


class SessionTemperatureStore:
    """Thread-safe, bounded map of session id -> temperature (LRU eviction)."""

    def __init__(self, max_sessions: int = MAX_SESSIONS) -> None:
        self._max = max(1, int(max_sessions))
        self._values: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session: object) -> float | None:
        key = session_key(session)
        if key is None:
            return None
        with self._lock:
            value = self._values.get(key)
            if value is not None:
                self._values.move_to_end(key)
            return value

    def set(self, session: object, value: object) -> float | None:
        """Store ``value`` (``None`` clears it); return what is now stored."""
        key = session_key(session)
        if key is None:
            raise InvalidInput("a temperature needs a conversation session; this turn has none")
        checked = check_temperature(value)
        with self._lock:
            if checked is None:
                self._values.pop(key, None)
                return None
            self._values[key] = checked
            self._values.move_to_end(key)
            while len(self._values) > self._max:
                self._values.popitem(last=False)
            return checked

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


STORE = SessionTemperatureStore()

USAGE = "usage: /temp [0-2|auto]  (shows the value with no argument)"


def temp_command(arg: object, session: object, *, lane: object = None,
                 store: SessionTemperatureStore | None = None) -> str:
    """Handle ``/temp``: show, set (``/temp 0.7``) or clear (``/temp auto``).

    ``lane`` is the value the calling surface uses when the session has none
    (the serve route's ``SONDER_SERVE_TEMPERATURE``), so the bare form shows
    what the next turn really sends.
    """
    target = STORE if store is None else store
    text = str(arg if arg is not None else "").strip()
    key = session_key(session)
    if not text:
        value = target.get(key)
        if value is not None:
            return "temperature: %s (source: session)" % format_temperature(value)
        lane_value = check_temperature(lane)
        if lane_value is not None:
            return ("temperature: %s (source: lane default; /temp sets this "
                    "session's own). %s" % (format_temperature(lane_value), USAGE))
        return ("temperature: auto (source: preset; the model's own sampling "
                "preset applies). " + USAGE)
    if key is None:
        return ("refused /temp: this turn has no conversation session to keep it on "
                "(pass a session id, e.g. session=\"main\")")
    try:
        value = parse_temperature_setting(text)
    except InvalidInput as error:
        return "refused /temp: %s. %s" % (error, USAGE)
    stored = target.set(key, value)
    if stored is None:
        return "temperature: auto for this session (the model preset applies)"
    return "temperature: %s for this session" % format_temperature(stored)


__all__ = [
    "MAX_SESSIONS",
    "STORE",
    "SessionTemperatureStore",
    "USAGE",
    "session_key",
    "temp_command",
]
