"""Per-endpoint TTL cache of Sonder Inference launch-profile metadata.

``SonderInferenceGateway.model_profiles`` reads ``/v1/models`` through this
cache, so the paths that ask for profile metadata (provider status, doctor,
and a chat turn whose estimate is above the context-fit floor) cost at most
one bounded GET per endpoint per ``SONDER_INFERENCE_PROFILE_TTL_SECONDS``
(default 60, range 0-3600).  Failures and a missing route are cached too, so
an Inference without ``/v1/models`` is not asked again until the TTL ends.

:func:`cached_catalog` never performs I/O: ``runtime_policy_status`` is
read-only and shows only what status or doctor already read.

Thread-safety: one lock guards the entry map; a second serializes refreshes
so concurrent callers past the TTL make one fetch (single flight).  The
catalog objects are frozen and their model maps read-only, so a returned
catalog can be shared between threads.
"""
from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType

from ...domain.inference_profiles import LaunchProfileCatalog

ENV_PROFILE_TTL = "SONDER_INFERENCE_PROFILE_TTL_SECONDS"
DEFAULT_PROFILE_TTL_SECONDS = 60.0
MAX_PROFILE_TTL_SECONDS = 3600.0


def profile_ttl_seconds(env: Mapping[str, str] | None = None) -> float:
    """The configured TTL; an invalid or out-of-range value keeps the default."""
    source = os.environ if env is None else env
    raw = str(source.get(ENV_PROFILE_TTL, "") or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_PROFILE_TTL_SECONDS
    except ValueError:
        return DEFAULT_PROFILE_TTL_SECONDS
    if value != value or not 0.0 <= value <= MAX_PROFILE_TTL_SECONDS:
        return DEFAULT_PROFILE_TTL_SECONDS
    return value


class ProfileCache:
    """Per-endpoint catalogs with a TTL and single-flight refresh."""

    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._refresh = threading.Lock()
        self._entries: dict[str, LaunchProfileCatalog] = {}

    def cached(self, base_url: str) -> LaunchProfileCatalog | None:
        """The last observation for ``base_url``, however old.  No I/O."""
        with self._lock:
            return self._entries.get(base_url)

    def _fresh(self, base_url: str, ttl: float, requested_at: float) -> LaunchProfileCatalog | None:
        cached = self.cached(base_url)
        if cached is None:
            return None
        # A catalog fetched after this request started (by a concurrent
        # caller) is fresh for it, whatever the TTL.
        if cached.checked_monotonic > requested_at or requested_at - cached.checked_monotonic < ttl:
            return cached
        return None

    def get(
        self, base_url: str, fetch: Callable[[], LaunchProfileCatalog], *,
        ttl: float, refresh: bool = False,
    ) -> LaunchProfileCatalog:
        """The cached catalog while fresh, else the result of one ``fetch``."""
        requested_at = self._monotonic()
        if not refresh:
            cached = self._fresh(base_url, ttl, requested_at)
            if cached is not None:
                return cached
        with self._refresh:
            if not refresh:
                cached = self._fresh(base_url, ttl, requested_at)
                if cached is not None:
                    return cached
            fetched = fetch()
            catalog = LaunchProfileCatalog(
                base_url=base_url, state=fetched.state, detail=fetched.detail,
                models=MappingProxyType(dict(fetched.models)),
                checked_monotonic=self._monotonic(),
            )
            with self._lock:
                self._entries[base_url] = catalog
            return catalog

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_CACHE = ProfileCache()


def shared_cache() -> ProfileCache:
    """The process-wide cache every gateway instance shares by default."""
    return _CACHE


def cached_catalog(base_url: str) -> LaunchProfileCatalog | None:
    """The shared cache's last observation for ``base_url`` (no I/O)."""
    return _CACHE.cached(base_url)


__all__ = [
    "DEFAULT_PROFILE_TTL_SECONDS",
    "ENV_PROFILE_TTL",
    "MAX_PROFILE_TTL_SECONDS",
    "ProfileCache",
    "cached_catalog",
    "profile_ttl_seconds",
    "shared_cache",
]
