"""Host tool inventory application service.

Owns caching, TTL, single-flight refresh, input validation and redacted
views.  It never touches the filesystem, environment or processes directly:
discovery and persistence arrive through ports, and the executable guard and
path redactor are injected callables owned by adapters.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

from ...domain.common.errors import DependencyUnavailable, InvalidInput
from ...domain.host_tools.model import (
    InventorySnapshot,
    InventoryView,
    ToolRecord,
    build_snapshot,
    build_view,
    capability_summary as _capability_summary,
    find_tool,
    is_stale,
)
from ..ports.host_tools import InventoryDiscovery, InventorySnapshotStore

_logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 86_400


class HostToolInventoryService:
    """Cached, validated access to the host developer-tool inventory."""

    def __init__(
        self,
        discovery: InventoryDiscovery,
        store: InventorySnapshotStore,
        *,
        clock: Callable[[], float],
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        redact_path: Callable[[str], str],
        executable_guard: Callable[[str], bool],
    ) -> None:
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        self._discovery = discovery
        self._store = store
        self._clock = clock
        self._ttl = ttl_seconds
        self._redact = redact_path
        self._guard = executable_guard
        self._lock = threading.Lock()
        self._snapshot: InventorySnapshot | None = None
        self._store_checked = False
        self._generation = 0

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    # -- snapshots ---------------------------------------------------------

    def _load_store_locked(self) -> None:
        if self._snapshot is None and not self._store_checked:
            self._store_checked = True
            try:
                self._snapshot = self._store.load()
            except Exception as error:  # the port promises not to raise
                _logger.warning("host tool snapshot load failed: %s", type(error).__name__)
                self._snapshot = None

    def snapshot(self, *, refresh: bool = False, full: bool = False) -> InventorySnapshot:
        """Return the current snapshot, discovering when missing/stale/forced.

        Concurrent callers share one discovery: a caller that waited for the
        lock while another caller discovered returns that fresh result.
        """
        observed = self._generation
        with self._lock:
            self._load_store_locked()
            current = self._snapshot
            fresh_elsewhere = self._generation != observed and current is not None
            needs = (
                current is None
                or is_stale(current, now=self._clock(), ttl_seconds=self._ttl)
                or ((refresh or full) and not fresh_elsewhere)
            )
            if not needs:
                assert current is not None
                return current
            try:
                discovered = self._discovery.discover(previous=None if full else current, full=full)
            except Exception as error:
                if current is not None:
                    _logger.warning("host tool discovery failed: %s", type(error).__name__)
                    note = f"refresh failed: {type(error).__name__}"
                    notes = tuple(n for n in current.notes if not n.startswith("refresh failed:"))
                    kept = build_snapshot(
                        os=current.os, os_release=current.os_release, machine=current.machine,
                        created_at=current.created_at, duration_ms=current.duration_ms,
                        tools=current.tools, notes=(*notes, note)[-16:], truncated=current.truncated,
                    )
                    self._snapshot = kept
                    return kept
                raise DependencyUnavailable("host tool discovery is unavailable") from error
            self._snapshot = discovered
            self._generation += 1
            try:
                self._store.save(discovered)
            except Exception as error:
                _logger.warning("host tool snapshot save failed: %s", type(error).__name__)
            return discovered

    def cached(self) -> InventorySnapshot | None:
        """Return the in-memory or persisted snapshot; never discovers."""
        with self._lock:
            self._load_store_locked()
            return self._snapshot

    # -- lookups and views -------------------------------------------------

    def lookup(self, name: str) -> ToolRecord | None:
        """Resolve a tool whose recorded path still passes the host guard."""
        record = find_tool(self.snapshot(), name)
        if record is None:
            return None
        try:
            allowed = bool(self._guard(record.path))
        except Exception:
            allowed = False
        return record if allowed else None

    def view(
        self,
        *,
        category: str | None = None,
        name: str | None = None,
        refresh: bool = False,
        redacted: bool = True,
    ) -> InventoryView:
        if category is not None and not isinstance(category, str):
            raise InvalidInput("category must be a string")
        if name is not None and not isinstance(name, str):
            raise InvalidInput("name must be a string")
        # Validate before any discovery so a bad query never costs a probe.
        build_view(
            InventorySnapshot(os="", os_release="", machine="", created_at=0.0,
                              duration_ms=0, tools=()),
            now=0.0, ttl_seconds=self._ttl, category=category, name=name,
            redact=lambda text: text,
        )
        snapshot = self.snapshot(refresh=refresh)
        redact = self._redact if redacted else (lambda text: text)
        return build_view(
            snapshot, now=self._clock(), ttl_seconds=self._ttl,
            category=category, name=name, redact=redact,
        )

    def capability_summary(self, *, max_chars: int = 480) -> str:
        """Compact path-free summary from the cached snapshot only; never raises."""
        try:
            return _capability_summary(self.cached(), max_chars=max_chars)
        except Exception:
            return ""


__all__ = ["DEFAULT_TTL_SECONDS", "HostToolInventoryService"]
