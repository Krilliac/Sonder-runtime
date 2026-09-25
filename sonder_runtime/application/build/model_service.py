"""The read-only build model service: locate, read, parse, cache.

Building a model never executes anything: the planner locates the project
and build tree inside the caller's roots, the reader returns bounded bytes,
and the pure domain parsers turn them into a ``BuildModel``. Models are cached
per principal (a principal never sees another principal's model, not even
through the brief) and keyed by a cheap fingerprint of the tree, so a
reconfigure or a rebuild of the compile database is seen on the next call.

``cached_summary`` is what the model-context brief calls on every chat turn:
it only looks in the cache and never reads or probes.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable

from ...domain.common.errors import InvalidInput
from ..context import OperationContext
from .ports import (
    BuildModelCache,
    BuildModelRequest,
    BuildPlanner,
    BuildTreeLocation,
    BuildTreeReader,
)

DEFAULT_TTL_SECONDS = 600
MAX_CACHED_MODELS = 8
MAX_CACHED_BYTES = 256 * 1024 * 1024
MAX_SUMMARY_CHARS = 600
VIEW_DETAILS = ("summary", "targets", "compile_units", "toolchain", "presets")
_MAX_VIEW_ITEMS = 500


def estimate_model_bytes(model: Any) -> int:
    """A deliberately generous size estimate of one parsed model."""
    units = len(getattr(model, "units", ()) or ())
    targets = len(getattr(model, "targets", ()) or ())
    presets = len(getattr(model, "presets", ()) or ())
    toolchains = len(getattr(model, "toolchains", ()) or ())
    return 16_384 + units * 1_024 + targets * 2_048 + (presets + toolchains) * 512


class LruBuildModelCache:
    """``BuildModelCache``: in-memory LRU bounded by count and estimated bytes."""

    def __init__(self, *, max_entries: int = MAX_CACHED_MODELS,
                 max_bytes: int = MAX_CACHED_BYTES,
                 estimate: Callable[[Any], int] = estimate_model_bytes) -> None:
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("cache bounds must be positive")
        self._max_entries = int(max_entries)
        self._max_bytes = int(max_bytes)
        self._estimate = estimate
        self._entries: "OrderedDict[tuple, tuple[Any, float, int]]" = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple) -> tuple[Any, float] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            self._entries.move_to_end(key)
            return entry[0], entry[1]

    def put(self, key: tuple, model: Any, *, stored_at: float) -> None:
        size = max(1, int(self._estimate(model)))
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[2]
            if size > self._max_bytes:
                return  # one model larger than the whole budget is never cached
            self._entries[key] = (model, float(stored_at), size)
            self._bytes += size
            while len(self._entries) > self._max_entries or self._bytes > self._max_bytes:
                _, (_, _, evicted) = self._entries.popitem(last=False)
                self._bytes -= evicted

    def entries_for(self, principal_id: str) -> tuple[tuple[tuple, Any, float], ...]:
        with self._lock:
            return tuple((key, model, stored_at)
                         for key, (model, stored_at, _) in self._entries.items()
                         if key and key[0] == principal_id)

    def invalidate(self, key: tuple) -> None:
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= old[2]

    def invalidate_tree(self, principal_id: str, project_root: str, build_dir: str) -> None:
        with self._lock:
            doomed = [key for key in self._entries
                      if key[:3] == (principal_id, project_root, build_dir)]
            for key in doomed:
                self._bytes -= self._entries.pop(key)[2]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def estimated_bytes(self) -> int:
        with self._lock:
            return self._bytes


class _Cached:
    __slots__ = ("model", "project_label", "build_label")

    def __init__(self, model: Any, project_label: str, build_label: str) -> None:
        self.model = model
        self.project_label = project_label
        self.build_label = build_label


class BuildModelService:
    def __init__(self, reader: BuildTreeReader, planner: BuildPlanner, cache: BuildModelCache, *,
                 clock: Callable[[], float], ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds < 0:
            raise ValueError("ttl_seconds must be a non-negative integer")
        self._reader = reader
        self._planner = planner
        self._cache = cache
        self._clock = clock
        self._ttl = ttl_seconds

    # -- models ----------------------------------------------------------------

    def locate(self, request: BuildModelRequest, context: OperationContext) -> BuildTreeLocation:
        return self._planner.locate(request, context)

    def model(self, request: BuildModelRequest, context: OperationContext, *,
              location: BuildTreeLocation | None = None) -> Any:
        if not isinstance(request, BuildModelRequest):
            raise InvalidInput("request must be a BuildModelRequest")
        location = location or self._planner.locate(request, context)
        fingerprint = self._reader.fingerprint(location.project_root, location.build_dir)
        key = (context.principal_id, location.project_root, location.build_dir,
               request.preset, fingerprint)
        if not request.refresh:
            hit = self._cache.get(key)
            if hit is not None:
                cached, stored_at = hit
                if self._clock() - stored_at <= self._ttl:
                    return cached.model
        model = self._planner.plan_model(request, context, location=location)
        self._cache.put(key, _Cached(model, location.project_label, location.build_label),
                        stored_at=self._clock())
        return model

    def cached_model(self, principal_id: str, project_root: str, build_dir: str) -> Any | None:
        """The newest cached model for a tree, without reading anything."""
        newest = None
        for key, cached, stored_at in self._cache.entries_for(principal_id):
            if key[1:3] == (project_root, build_dir) and (newest is None or stored_at > newest[1]):
                newest = (cached.model, stored_at)
        return None if newest is None else newest[0]

    def invalidate(self, principal_id: str, project_root: str, build_dir: str) -> None:
        invalidate_tree = getattr(self._cache, "invalidate_tree", None)
        if callable(invalidate_tree):
            invalidate_tree(principal_id, project_root, build_dir)
            return
        for key, _, _ in self._cache.entries_for(principal_id):
            if key[1:3] == (project_root, build_dir):
                self._cache.invalidate(key)

    def view(self, request: BuildModelRequest, context: OperationContext, *,
             detail: str = "summary", target: str = "", max_items: int = 100) -> dict:
        """The label-only wire view of one model."""
        if detail not in VIEW_DETAILS:
            raise InvalidInput("detail must be one of %s" % ", ".join(VIEW_DETAILS))
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= _MAX_VIEW_ITEMS:
            raise InvalidInput("max_items must be within 1..%d" % _MAX_VIEW_ITEMS)
        from ...domain.build.model import build_view, model_to_wire

        model = self.model(request, context)
        return model_to_wire(build_view(model, detail=detail, target=str(target or ""),
                                        max_items=max_items))

    # -- brief -----------------------------------------------------------------

    def cached_summary(self, principal_id: str, project_label: str = "") -> str:
        """One-line build summary for the brief; cache only, never reads or probes."""
        if not isinstance(principal_id, str) or not principal_id:
            return ""
        newest: tuple[_Cached, float] | None = None
        for _, cached, stored_at in self._cache.entries_for(principal_id):
            if project_label and cached.project_label != project_label:
                continue
            if newest is None or stored_at > newest[1]:
                newest = (cached, stored_at)
        if newest is None:
            return ""
        from ...domain.build.model import build_context_summary

        try:
            text = build_context_summary(newest[0].model, max_chars=MAX_SUMMARY_CHARS)
        except (TypeError, ValueError):
            return ""
        return str(text or "")[:MAX_SUMMARY_CHARS]


__all__ = [
    "BuildModelService", "DEFAULT_TTL_SECONDS", "LruBuildModelCache", "MAX_CACHED_BYTES",
    "MAX_CACHED_MODELS", "VIEW_DETAILS", "estimate_model_bytes",
]
