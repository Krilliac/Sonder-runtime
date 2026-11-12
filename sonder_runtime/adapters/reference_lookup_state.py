"""Index handle, kill switch, last error and status for the reference lookup.

Kept apart from ``reference_lookup_service`` so the runtime policy status and
``diagnostics`` lines can read the lookup state without the policy adapter and
the service importing each other.  This module never reads the runtime policy:
callers pass the rule they hold.  Thread-safe: shared state (the index
instance and the last error) is guarded by a lock; each search opens its own
connection.  Nothing here raises to a caller.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

from sonder_runtime.adapters.reference_index import ReferenceIndex
from sonder_runtime.domain.reference_lookup.applies import AppliesRule
from sonder_runtime.domain.reference_lookup.models import IndexState
from sonder_runtime.domain.reference_lookup.sanitize import one_line, sanitize
from sonder_runtime.platform import reference_library_config as _config

_LOCK = threading.Lock()
_INDEX: tuple[str, ReferenceIndex] | None = None
_LAST_ERROR = ""


@dataclass(frozen=True)
class LookupStatus:
    kill_switch_off: bool
    policy_enabled: bool
    index_path: str
    path_source: str
    state: IndexState
    passages: int
    sources: tuple[tuple[str, int], ...]
    built_at: str
    detail: str
    names: tuple[str, ...]
    prefixes: tuple[str, ...]
    last_error: str

    @property
    def readable(self) -> bool:
        return self.state is IndexState.READY


def set_last_error(text: str) -> None:
    global _LAST_ERROR
    with _LOCK:
        _LAST_ERROR = text


def last_error() -> str:
    with _LOCK:
        return _LAST_ERROR


def index() -> ReferenceIndex:
    """The index adapter for the configured path (one instance per path, so its status cache lives)."""
    global _INDEX
    path = _config.load().index_path
    with _LOCK:
        if _INDEX is None or _INDEX[0] != str(path):
            _INDEX = (str(path), ReferenceIndex(path))
        return _INDEX[1]


def kill_switch_off() -> bool:
    try:
        return not _config.lookup_enabled()
    except Exception:  # noqa: BLE001  # a broken environment read leaves lookup on
        return False


def status(rule: AppliesRule) -> LookupStatus:
    """Index and rule status for the status lines; never raises."""
    try:
        config = _config.load()
        path, source = str(config.index_path), config.source.value
        found = index().status()
        state, passages, sources = found.state, found.passages, found.sources
        built_at, detail = found.built_at, found.detail
    except Exception as exc:  # noqa: BLE001
        path, source, state, passages, sources, built_at = "", "", IndexState.ERROR, 0, (), ""
        detail = sanitize(f"{type(exc).__name__}: {exc}")
    return LookupStatus(
        kill_switch_off=kill_switch_off(), policy_enabled=rule.enabled, index_path=path,
        path_source=source, state=state, passages=passages, sources=sources, built_at=built_at,
        detail=detail, names=tuple(sorted(rule.names)), prefixes=rule.prefixes,
        last_error=last_error(),
    )


def status_line(rule: AppliesRule) -> str:
    """One line for the runtime policy status output and ``diagnostics``."""
    s = status(rule)
    if s.kill_switch_off:
        automatic = "off (SONDER_REFERENCE_LOOKUP=0)"
    elif not s.policy_enabled:
        automatic = "off (runtime policy)"
    else:
        automatic = f"names={', '.join(s.names) or '-'} prefixes={', '.join(s.prefixes) or '-'}"
    if s.readable:
        sources = ", ".join(f"{name}:{count}" for name, count in s.sources) or "-"
        index_text = f"readable=yes passages={s.passages} sources={sources} built={s.built_at or '-'}"
    else:
        index_text = f"readable=no ({s.state.value}{': ' + s.detail if s.detail else ''})"
    return one_line(
        f"index={s.index_path or '-'} {index_text}; automatic lookup {automatic}; "
        f"last error: {s.last_error or 'none'}"
    )
