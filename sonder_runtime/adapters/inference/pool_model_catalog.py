"""Resolve a model pin against the whole Ollama worker pool's catalog.

A pooled ``/api/tags`` read describes whichever single member answered, so a
model installed only on a worker looks absent.  Generation already routes to a
member that reports the model; only the pin's *discovery* needs the pool-wide
union.  The caller supplies every effect (endpoint lookup, per-member tag
fetch, non-chat check) so this module performs no network I/O itself.
"""
from __future__ import annotations

import http.client
import logging
import socket
import threading
import time
import urllib.error
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ...domain.model_catalog import catalog_records, resolve_record

# Transport-shaped failures of a per-member tag read (URLError is an OSError;
# a bad JSON body is a ValueError).  Programming errors are deliberately absent.
_TRANSPORT_ERRORS = (OSError, ValueError, http.client.HTTPException)

logger = logging.getLogger(__name__)


class PinStatus(str, Enum):
    RESOLVED = "resolved"
    NOT_INSTALLED = "not_installed"
    NOT_CHAT_CAPABLE = "not_chat_capable"
    CATALOG_UNAVAILABLE = "catalog_unavailable"


class PinSource(str, Enum):
    NONE = ""
    ENDPOINT = "endpoint"
    POOL = "pool"


@dataclass(frozen=True)
class PinResolution:
    status: PinStatus
    model: str | None = None
    record: Mapping[str, Any] | None = None
    source: PinSource = PinSource.NONE

    @property
    def ok(self) -> bool:
        return self.status is PinStatus.RESOLVED


class PoolCatalogCache:
    """Short-lived, thread-safe cache of successful pool catalog unions.

    The loader runs outside the lock so a slow member read never blocks other
    callers; concurrent misses may each load, and the last store wins.  Failed
    and empty loads are never cached.
    """

    def __init__(
        self, ttl_seconds: float = 15.0, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[Hashable, tuple[float, Mapping[str, Any]]] = {}

    def get(
        self, key: Hashable, loader: Callable[[], Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], bool]:
        """Return ``(union, fresh)``; ``fresh`` is False when served from cache."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry[0] < self._ttl:
                return entry[1], False
        return self.refresh(key, loader), True

    def refresh(
        self, key: Hashable, loader: Callable[[], Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Load unconditionally, then cache the result when it is a non-empty union."""
        payload = loader()
        if isinstance(payload, Mapping) and payload.get("models"):
            now = self._clock()
            with self._lock:
                self._entries = {
                    k: v for k, v in self._entries.items()
                    if now - v[0] < self._ttl and k != key
                }
                self._entries[key] = (now, payload)
        return payload

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _is_unreachable_error(error: BaseException) -> bool:
    """True only when the primary host could not be reached, never when busy."""
    if isinstance(error, (TimeoutError, socket.timeout)):
        return False
    name = type(error).__name__
    if name in ("GpuAdmissionRefused", "WorkerPoolBackpressure", "WorkerPoolDraining"):
        return False
    if isinstance(error, urllib.error.URLError):
        if name == "WorkerPoolUnavailable":
            return True
        reason = getattr(error, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return False
        if isinstance(reason, str) and "timed out" in reason.lower():
            return False
        return True
    return isinstance(
        error,
        (ConnectionRefusedError, ConnectionResetError, ConnectionAbortedError, BrokenPipeError),
    )


def discovery_tags(
    pool: Any,
    get_tags: Callable[[], object],
    fetch_member_tags: Callable[[str], object],
    *,
    host_origin: str | None = None,
) -> object:
    """The ``/api/tags`` payload that decides which models are *known*.

    A pooled ``get_tags`` read describes whichever single member the scheduler
    picked, so a busy member's models would flicker out of the catalog.  With
    the pool enabled and ``host_origin is None``, read the union over every
    routable member (skipping open-circuit workers); the primary wins a name
    several members hold.

    ``host_origin`` narrows a pooled read to that one member (this host), which
    keeps lanes that must not gain a worker-only name deterministic.  When that
    member is not an admissible pool member (e.g. an external roster without
    ``local_fallback``), or its read fails because the host is unreachable
    (connection refused/reset, ``WorkerPoolUnavailable``), the pooled
    ``get_tags`` read is used.  Busy errors (``TimeoutError``,
    ``GpuAdmissionRefused``, ``WorkerPoolBackpressure``) are NEVER treated as
    unreachable and re-raise directly so a busy primary does not fall back to a
    worker's narrower catalog.
    """
    if pool is not None and pool.enabled:
        if host_origin is None:
            union = pool.catalog_union(fetch_member_tags, routable_only=True)
            if isinstance(union, Mapping) and union.get("models"):
                return union
            return {"models": []}
        if pool.catalog_member(host_origin):
            try:
                return fetch_member_tags(host_origin)
            except Exception as error:
                if not _is_unreachable_error(error):
                    raise
                logger.warning(
                    "host catalog read failed (%s); falling back to pooled read",
                    type(error).__name__,
                )
        else:
            logger.info(
                "host origin is not an admissible pool member; using pooled catalog read",
            )
    return get_tags()


def resolve_model_pin(
    selector: object,
    *,
    endpoint_record: Callable[[object], tuple[str, Mapping[str, Any]] | None],
    pool: Any,
    fetch_member_tags: Callable[[str], object],
    nonchat_reason: Callable[[Mapping[str, Any]], object],
    cache: PoolCatalogCache,
) -> PinResolution:
    """Resolve ``selector`` via the endpoint first, then the pool-wide union.

    A refusal is never served from a cached union: if the union behind a
    NOT_INSTALLED / NOT_CHAT_CAPABLE verdict came from the cache, it is
    reloaded once and the selector is re-evaluated.
    """
    try:
        found = endpoint_record(selector)
    except Exception:  # the endpoint read failing is a "no" today; keep that.
        found = None
    if found is not None:
        if not nonchat_reason(found[1]):
            return PinResolution(
                PinStatus.RESOLVED, found[0], found[1], PinSource.ENDPOINT)
        endpoint_verdict = PinStatus.NOT_CHAT_CAPABLE
    else:
        endpoint_verdict = PinStatus.NOT_INSTALLED

    if pool is None or not pool.enabled:
        return PinResolution(endpoint_verdict)

    key = tuple(pool.origins)

    def load() -> Mapping[str, Any]:
        return pool.catalog_union(fetch_member_tags, routable_only=True)

    def evaluate(union: Mapping[str, Any]) -> PinResolution:
        pooled = resolve_record(selector, catalog_records(union))
        if pooled is None:
            return PinResolution(endpoint_verdict)
        if nonchat_reason(pooled[1]):
            return PinResolution(PinStatus.NOT_CHAT_CAPABLE)
        return PinResolution(PinStatus.RESOLVED, pooled[0], pooled[1], PinSource.POOL)

    try:
        union, fresh = cache.get(key, load)
        verdict = evaluate(union)
        if not verdict.ok and not fresh:
            verdict = evaluate(cache.refresh(key, load))
    except _TRANSPORT_ERRORS:
        if endpoint_verdict is PinStatus.NOT_INSTALLED:
            return PinResolution(PinStatus.CATALOG_UNAVAILABLE)
        return PinResolution(endpoint_verdict)
    return verdict


def pool_model_capabilities(pool: Any) -> dict[str, dict[str, Any]]:
    """Aggregate per-model capability metadata from cached pool snapshots.

    Collects known origins, currently resident origins, highest advertised
    context window, tool-calling support, and MoE active-parameter counts
    without triggering synchronous probes.
    """
    if pool is None or not callable(getattr(pool, "snapshots", None)):
        return {}
    aggregated: dict[str, dict[str, Any]] = {}
    for snap in pool.snapshots():
        if not getattr(snap, "healthy", False):
            continue
        models = getattr(snap, "models", ()) or ()
        loaded_keys = {
            str(m).strip().casefold()
            for m in (getattr(snap, "loaded_models", ()) or ())
            if str(m).strip()
        }
        contexts = getattr(snap, "model_contexts", {}) or {}
        tools_map = getattr(snap, "supports_tools", {}) or {}
        active_map = getattr(snap, "model_active_billions", {}) or {}
        for model_name in models:
            entry = aggregated.setdefault(
                model_name,
                {
                    "origins": [],
                    "resident_origins": [],
                    "max_context": None,
                    "supports_tools": None,
                    "active_billions": None,
                },
            )
            if snap.origin not in entry["origins"]:
                entry["origins"].append(snap.origin)
            if model_name.strip().casefold() in loaded_keys and snap.origin not in entry["resident_origins"]:
                entry["resident_origins"].append(snap.origin)
            ctx = contexts.get(model_name)
            if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
                if entry["max_context"] is None or ctx > entry["max_context"]:
                    entry["max_context"] = ctx
            if model_name in tools_map and isinstance(tools_map[model_name], bool):
                entry["supports_tools"] = (
                    True if entry["supports_tools"] is True or tools_map[model_name] else False
                )
            active_val = active_map.get(model_name)
            if isinstance(active_val, (int, float)) and not isinstance(active_val, bool) and active_val > 0:
                if entry["active_billions"] is None:
                    entry["active_billions"] = float(active_val)
    return aggregated
