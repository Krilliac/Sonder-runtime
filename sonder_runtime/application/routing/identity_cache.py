"""Bounded host-identity observations shared by capability routing layers."""
from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Hashable
    from typing import Protocol

    from sonder_runtime.domain.routing.backend_conformance import BackendIdentity

    class _EvidenceSource(Protocol):
        @property
        def revision(self) -> object: ...


IDENTITY_CACHE_TTL_SECONDS = 60.0
_MAX_IDENTITY_CACHE_ENTRIES = 64


class IdentityObservationCache:
    """Cache successes and unavailable probes; evidence refreshes invalidate both."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: dict[Hashable, tuple[float, object, BackendIdentity | None]] = {}
        self._lock = threading.Lock()

    def observe(
        self,
        key: Hashable,
        observer: Callable[[], BackendIdentity | None],
        *,
        evidence: _EvidenceSource | None = None,
    ) -> BackendIdentity | None:
        try:
            # A production caller may change state homes without restarting.
            key = (getattr(evidence, "path", None), key)
            with self._lock:
                revision = evidence.revision if evidence is not None else None
                now = self._clock()
                cached = self._entries.get(key)
                if cached is not None:
                    observed_at, saved_revision, identity = cached
                    if saved_revision == revision and 0 <= now - observed_at < IDENTITY_CACHE_TTL_SECONDS:
                        return identity
                try:
                    identity = observer()
                except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
                    identity = None
                if len(self._entries) >= _MAX_IDENTITY_CACHE_ENTRIES:
                    self._entries.pop(next(iter(self._entries)))
                # Use the pre-observation revision so a concurrent refresh is
                # detected on the next lookup, even when discovery failed.
                self._entries[key] = (self._clock(), revision, identity)
                return identity
        except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
            return None
