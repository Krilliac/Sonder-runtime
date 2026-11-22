"""Per-session reasoning effort, set with ``/effort`` or the UI selector.

Mirrors :mod:`sonder_runtime.application.session.temperature_settings`:
stored per conversation thread in a bounded, owner-fair LRU map. Clearing
with ``/effort auto`` removes the entry so the model/provider default applies.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

from ...domain.common.errors import InvalidInput
from ...domain.reasoning_effort import (
    EFFORT_LEVELS,
    check_effort,
    format_effort,
    parse_effort_setting,
)

MAX_SESSIONS = 4096
MAX_OWNER_SESSIONS = 256
_NO_SESSION = frozenset({"", "none"})


def session_key(session: object) -> str | None:
    key = str(session if session is not None else "").strip()
    return None if key.lower() in _NO_SESSION else key


def _owner_key(owner: object) -> str:
    return str(owner if owner is not None else "").strip()


class SessionEffortStore:
    """Thread-safe, bounded map of session id -> reasoning effort."""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        max_per_owner: int = MAX_OWNER_SESSIONS,
    ) -> None:
        self._max = max(1, int(max_sessions))
        self._max_per_owner = max(1, min(self._max, int(max_per_owner)))
        self._values: OrderedDict[str, str] = OrderedDict()
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
        counts: dict[str, int] = {}
        for owner_key in self._owners.values():
            counts[owner_key] = counts.get(owner_key, 0) + 1
        if counts:
            heaviest = max(counts.values())
            for candidate in list(self._values):
                owner_key = self._owners.get(candidate, "")
                if counts.get(owner_key, 0) == heaviest:
                    self._values.pop(candidate, None)
                    self._owners.pop(candidate, None)
                    return
        evicted_key, _ = self._values.popitem(last=False)
        self._owners.pop(evicted_key, None)

    def get(self, session: object) -> str | None:
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
    ) -> str | None:
        key = session_key(session)
        if key is None:
            raise InvalidInput("reasoning effort needs a conversation session; this turn has none")
        checked = check_effort(value)
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


STORE = SessionEffortStore()


def effort_command(
    arg: object,
    session: object,
    *,
    lane: object = None,
    store: SessionEffortStore = STORE,
    owner: object = "",
) -> str:
    """Handle ``/effort [off|minimal|low|medium|high|xhigh|auto]``."""
    raw = str(arg if arg is not None else "").strip()
    levels_hint = "|".join(EFFORT_LEVELS) + "|auto"
    if not raw:
        current = store.get(session)
        lane_eff = check_effort(lane) if lane is not None else None
        if current is not None:
            return (
                "reasoning effort: %s (this session). Change with /effort <%s>."
                % (format_effort(current), levels_hint)
            )
        if lane_eff is not None:
            return (
                "reasoning effort: auto (lane default %s). Change with /effort <%s>."
                % (format_effort(lane_eff), levels_hint)
            )
        return (
            "reasoning effort: auto (model default). Change with /effort <%s>."
            % levels_hint
        )
    try:
        parsed = parse_effort_setting(raw)
        stored = store.set(session, parsed, owner=owner)
    except InvalidInput as exc:
        return "ERROR: %s" % exc
    if stored is None:
        return "reasoning effort: auto (model default) for this session."
    return "reasoning effort: %s for this session." % format_effort(stored)


__all__ = [
    "MAX_OWNER_SESSIONS",
    "MAX_SESSIONS",
    "STORE",
    "SessionEffortStore",
    "effort_command",
    "session_key",
]
