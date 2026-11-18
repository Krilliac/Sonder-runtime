"""Per-session sampling temperature, set with ``/temp`` or the GUI slider.

The value lives with the conversation thread (keyed by its session id), not
in a global: two threads can run at different temperatures.  It is held in
memory for the life of the process; a restart returns every thread to
``auto`` (the lane default or model preset).

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
MAX_OWNER_SESSIONS = 256
# Session ids that mean "no thread": a one-off turn has nowhere to keep a value.
_NO_SESSION = frozenset({"", "none"})


def session_key(session: object) -> str | None:
    """The store key for ``session``, or ``None`` when there is no thread."""
    key = str(session if session is not None else "").strip()
    return None if key.lower() in _NO_SESSION else key


def _owner_key(owner: object) -> str:
    return str(owner if owner is not None else "").strip()


class SessionTemperatureStore:
    """Thread-safe, bounded map of session id -> temperature (owner-fair LRU)."""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        max_per_owner: int = MAX_OWNER_SESSIONS,
    ) -> None:
        self._max = max(1, int(max_sessions))
        self._max_per_owner = max(1, min(self._max, int(max_per_owner)))
        self._values: OrderedDict[str, float] = OrderedDict()
        self._owners: dict[str, str] = {}
        self._lock = threading.Lock()

    def _evict_oldest_for_owner(self, owner_key: str) -> bool:
        for candidate in list(self._values):
            if self._owners.get(candidate) == owner_key:
                self._values.pop(candidate, None)
                self._owners.pop(candidate, None)
                return True
        return False

    def _evict_for_global_cap(self) -> None:
        # Evict the oldest entry belonging to the principal holding the most
        # slots so one noisy tenant cannot push out a quiet tenant's session.
        counts: dict[str, int] = {}
        for owner_key in self._owners.values():
            counts[owner_key] = counts.get(owner_key, 0) + 1
        if counts:
            heaviest_count = max(counts.values())
            for candidate in list(self._values):
                owner_key = self._owners.get(candidate, "")
                if counts.get(owner_key, 0) == heaviest_count:
                    self._values.pop(candidate, None)
                    self._owners.pop(candidate, None)
                    return
        evicted_key, _ = self._values.popitem(last=False)
        self._owners.pop(evicted_key, None)

    def get(self, session: object) -> float | None:
        key = session_key(session)
        if key is None:
            return None
        with self._lock:
            value = self._values.get(key)
            if value is not None:
                self._values.move_to_end(key)
            return value

    def set(
        self, session: object, value: object, *, owner: object = "",
    ) -> float | None:
        """Store ``value`` (``None`` clears it); return what is now stored."""
        key = session_key(session)
        if key is None:
            raise InvalidInput("a temperature needs a conversation session; this turn has none")
        checked = check_temperature(value)
        owner_key = _owner_key(owner)
        with self._lock:
            if checked is None:
                self._values.pop(key, None)
                self._owners.pop(key, None)
                return None
            if key in self._values:
                self._values[key] = checked
                self._owners[key] = owner_key
                self._values.move_to_end(key)
                return checked
            if owner_key:
                owned = sum(
                    1 for candidate_owner in self._owners.values()
                    if candidate_owner == owner_key
                )
                while owned >= self._max_per_owner:
                    if not self._evict_oldest_for_owner(owner_key):
                        break
                    owned -= 1
            while len(self._values) >= self._max:
                self._evict_for_global_cap()
            self._values[key] = checked
            self._owners[key] = owner_key
            self._values.move_to_end(key)
            return checked

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._owners.clear()


STORE = SessionTemperatureStore()

USAGE = "usage: /temp [0-2|auto]  (shows the value with no argument)"


def temp_command(arg: object, session: object, *, lane: object = None,
                 store: SessionTemperatureStore | None = None,
                 owner: object = "") -> str:
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
    stored = target.set(key, value, owner=owner)
    if stored is None:
        if lane is not None:
            return ("temperature: auto for this session (session override cleared; "
                    "use the lane default or model preset)")
        return "temperature: auto for this session (the model preset applies)"
    return "temperature: %s for this session" % format_temperature(stored)


__all__ = [
    "MAX_OWNER_SESSIONS",
    "MAX_SESSIONS",
    "STORE",
    "SessionTemperatureStore",
    "USAGE",
    "session_key",
    "temp_command",
]
