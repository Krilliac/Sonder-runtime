"""Legacy HTTP chat steps served through the ModelGateway for non-Ollama rungs.

The legacy chat path in ``server.py`` is Ollama-shaped.  When a rung's tier is
bound to another provider (``SONDER_*_PROVIDER`` / ``SONDER_MODEL_BACKEND``),
the local branch of ``_chat_request`` hands the payload to the application's
``model_gateway`` through this module; conversion, shaping and error
classification live in ``sonder_runtime/application/chat/provider_bridge.py``.
With every tier on Ollama none of this runs.  ``server.py`` keeps only thin
wrappers that inject its live policy (cloud consent, Ollama locality, the
application graph), so a live reload of ``server.py`` never forks this state.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
import dataclasses
import logging
import os
import threading
import time
from typing import cast
import weakref
from urllib.parse import urlsplit, urlunsplit

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.chat import provider_bridge
from sonder_runtime.application.context import (
    OperationContext,
    current_operation_context,
    local_owner_context,
)
from sonder_runtime.domain.common.errors import Cancelled, SonderError
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread

_logger = logging.getLogger("sonder.server")

# Bounded TTL / single-flight cache for Sonder Inference model advertisement
# lookups on the legacy chat hot path.  Warm hits never touch the network;
# stale positive entries trigger a non-blocking single-flight background
# refresh; cold or hard-expired misses coalesce onto one probe bounded by a
# strict short wait budget so a slow or unreachable remote worker never stalls
# chat turns.
MODEL_PROBE_POSITIVE_TTL_SECONDS = 30.0
MODEL_PROBE_NEGATIVE_TTL_SECONDS = 5.0
MODEL_PROBE_STALE_TTL_SECONDS = 120.0
MODEL_PROBE_WAIT_TIMEOUT_SECONDS = 0.5
MODEL_PROBE_MAX_CACHE_ENTRIES = 128
MODEL_PROBE_MAX_MODELS_PER_SCOPE = 2048
MODEL_PROBE_MAX_INFLIGHT = 16
_MODEL_PROBE_MAX_NAME_LENGTH = 256

_ScopeKey = tuple[int, str, str, tuple[str, ...]]


def _safe_url_origin(raw: object, *, include_path: bool = False) -> str:
    """Extract a credential-free ``scheme://host[:port]`` origin for keying/logs.

    When ``include_path`` is True, preserve the URL path (for provider configs
    whose ``display_base_url`` property already enforces a safe path policy).
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None:
        host = f"{host}:{port}"
    path = parts.path if include_path else ""
    return urlunsplit((parts.scheme.lower(), host, path, "", ""))


def _extract_model_name(raw: object) -> str:
    """Normalize a model entry (either a string id or a ``{"id": ...}`` mapping)."""
    if isinstance(raw, Mapping):
        raw = raw.get("id")
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    if not text or len(text) > _MODEL_PROBE_MAX_NAME_LENGTH:
        return ""
    return text


def _iter_non_string(value: object) -> Iterable[object]:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return value
    return ()


def _gateway_probe_scope(gw: object) -> _ScopeKey:
    """Derive a safe cache key scoped to gateway identity and configured endpoints.

    Never reads or stores credentials, query parameters, or fragments.
    """
    endpoint_origin = ""
    worker_origins: tuple[str, ...] = ()
    settings_fn = getattr(gw, "settings", None)
    if not callable(settings_fn):
        settings_fn = getattr(gw, "_resolved_config", None)
    if callable(settings_fn):
        try:
            config = settings_fn()
        except Exception as exc:  # noqa: BLE001 - invalid/unready config still scopes safely
            endpoint_origin = "(config-error:%s)" % type(exc).__name__
        else:
            display = getattr(config, "display_base_url", None)
            raw_base = display or getattr(config, "base_url", "")
            endpoint_origin = _safe_url_origin(raw_base)
            raw_workers = getattr(config, "workers", None)
            if isinstance(raw_workers, (list, tuple)):
                origins: list[str] = []
                for spec in raw_workers:
                    raw_url = getattr(spec, "display_url", None) or getattr(spec, "url", "")
                    origin = _safe_url_origin(raw_url)
                    if origin:
                        origins.append(origin)
                worker_origins = tuple(sorted(set(origins)))
    gw_type = "%s.%s" % (type(gw).__module__, type(gw).__qualname__)
    return (id(gw), gw_type, endpoint_origin, worker_origins)


def _gateway_identity_ref(
    gw: object,
) -> tuple[str, weakref.ReferenceType[object] | None]:
    """Weak identity token preventing ``id(gw)`` reuse aliasing after GC."""
    try:
        return ("gw", weakref.ref(gw))
    except TypeError:
        pass
    # Non-weakrefable test doubles (e.g. SimpleNamespace) often attach a unique
    # callable directly on their instance ``__dict__``; track that callable
    # weakly so GC'd namespaces cannot alias fresh ones at the same address.
    # For ``__slots__`` classes with normal methods, ``getattr`` allocates an
    # ephemeral bound ``MethodType`` whose weakref dies immediately — fall back
    # to ``("none", None)`` so cache hits remain effective.
    instance_dict = getattr(gw, "__dict__", None)
    if isinstance(instance_dict, Mapping) and "unified_worker_status" in instance_dict:
        unified = instance_dict["unified_worker_status"]
        if callable(unified):
            try:
                return ("unified", weakref.ref(unified))
            except TypeError:
                pass
    return ("none", None)


@dataclasses.dataclass(frozen=True, slots=True)
class _CatalogEntry:
    exact_cf: frozenset[str]
    prefixes_cf: frozenset[str]
    probed_ok: bool
    updated_at: float
    failed_at: float | None
    ref_kind: str
    ref: weakref.ReferenceType[object] | None

    def matches_gateway(self, gw: object) -> bool:
        if self.ref_kind == "gw":
            return self.ref is not None and self.ref() is gw
        if self.ref_kind == "unified":
            return (
                self.ref is not None
                and self.ref() is getattr(gw, "unified_worker_status", None)
            )
        return True

    def matches_model(self, wanted: str) -> bool:
        if not self.probed_ok:
            return False
        wanted_cf = wanted.casefold()
        if wanted_cf in self.exact_cf:
            return True
        if ":" not in wanted:
            return wanted_cf in self.prefixes_cf
        return False


def _build_catalog_entry(
    models: Iterable[str],
    *,
    probed_ok: bool,
    updated_at: float,
    failed_at: float | None,
    ref_kind: str,
    ref: weakref.ReferenceType[object] | None,
) -> _CatalogEntry:
    exact_cf = frozenset(item.casefold() for item in models if item)
    prefixes_cf = frozenset(
        item_cf.split(":", 1)[0] for item_cf in exact_cf if ":" in item_cf
    )
    return _CatalogEntry(
        exact_cf=exact_cf,
        prefixes_cf=prefixes_cf,
        probed_ok=probed_ok,
        updated_at=updated_at,
        failed_at=failed_at,
        ref_kind=ref_kind,
        ref=ref,
    )


class _InFlightProbe:
    __slots__ = ("done", "generation", "deadline", "timed_out", "entry", "worker")

    def __init__(self, generation: int, deadline: float) -> None:
        self.done = threading.Event()
        self.generation = generation
        self.deadline = deadline
        self.timed_out = False
        self.entry: _CatalogEntry | None = None
        self.worker: threading.Thread | None = None


def _probe_gateway_models(gw: object) -> tuple[tuple[str, ...], bool]:
    """Probe ``gw`` for models advertised by ``sonder_inference`` workers or health.

    Preserves the gateway's own endpoint-policy and security boundaries; never
    opens raw sockets or logs credentials.
    """
    candidates: list[str] = []
    seen_cf: set[str] = set()
    status_ok = False
    has_unhealthy_worker = False

    def _add_model(raw: object) -> None:
        name = _extract_model_name(raw)
        if not name:
            return
        cf = name.casefold()
        if cf in seen_cf or len(candidates) >= MODEL_PROBE_MAX_MODELS_PER_SCOPE:
            return
        seen_cf.add(cf)
        candidates.append(name)

    unified = getattr(gw, "unified_worker_status", None)
    if callable(unified):
        try:
            status = unified()
        except Exception as exc:  # noqa: BLE001 - probe failures become negative cache entries
            _logger.debug(
                "sonder_inference worker status probe failed: %s",
                type(exc).__name__,
            )
            status = None
        else:
            if isinstance(status, Mapping):
                status_ok = True
                for row in _iter_non_string(status.get("workers")):
                    if isinstance(row, Mapping) and row.get("source") == "sonder_inference_worker":
                        if row.get("healthy") is False:
                            has_unhealthy_worker = True
                        for raw in _iter_non_string(row.get("models")):
                            _add_model(raw)

    if not candidates:
        health_fn = getattr(gw, "health", None)
        if callable(health_fn):
            try:
                snap = health_fn()
            except Exception as exc:  # noqa: BLE001 - probe failures become negative cache entries
                _logger.debug(
                    "sonder_inference health probe failed: %s",
                    type(exc).__name__,
                )
            else:
                state = getattr(snap, "state", None)
                doc = getattr(snap, "document", None)
                if isinstance(doc, Mapping):
                    for raw in _iter_non_string(doc.get("models")):
                        _add_model(raw)
                if state == "unavailable":
                    status_ok = False
                elif candidates or state in ("ready", "degraded", None):
                    status_ok = True

    if not candidates and has_unhealthy_worker:
        status_ok = False
    return tuple(candidates), status_ok


class _ModelAdvertisementCache:
    """Thread-safe bounded LRU TTL cache with single-flight background probes."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        positive_ttl_seconds: float = MODEL_PROBE_POSITIVE_TTL_SECONDS,
        negative_ttl_seconds: float = MODEL_PROBE_NEGATIVE_TTL_SECONDS,
        stale_ttl_seconds: float = MODEL_PROBE_STALE_TTL_SECONDS,
        wait_timeout_seconds: float = MODEL_PROBE_WAIT_TIMEOUT_SECONDS,
        max_entries: int = MODEL_PROBE_MAX_CACHE_ENTRIES,
        max_inflight: int = MODEL_PROBE_MAX_INFLIGHT,
    ) -> None:
        if positive_ttl_seconds < 0.0 or negative_ttl_seconds < 0.0 or stale_ttl_seconds < 0.0:
            raise ValueError("probe cache TTLs must be non-negative")
        if wait_timeout_seconds <= 0.0:
            raise ValueError("probe wait timeout must be positive")
        if max_entries < 1 or max_inflight < 1:
            raise ValueError("probe cache capacities must be at least 1")
        self._monotonic = monotonic
        self._positive_ttl_seconds = float(positive_ttl_seconds)
        self._negative_ttl_seconds = float(negative_ttl_seconds)
        self._stale_ttl_seconds = float(stale_ttl_seconds)
        self._wait_timeout_seconds = float(wait_timeout_seconds)
        self._max_entries = int(max_entries)
        self._max_inflight = int(max_inflight)
        self._lock = threading.Lock()
        self._entries: OrderedDict[_ScopeKey, _CatalogEntry] = OrderedDict()
        self._inflight: dict[_ScopeKey, _InFlightProbe] = {}
        self._generation = 0

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def inflight_count(self) -> int:
        with self._lock:
            self._reap_dead_inflight_locked()
            return len(self._inflight)

    def _reap_dead_inflight_locked(self) -> None:
        """Evict flights whose started thread exited without invoking ``_run_probe``.

        ``_ManagedThread.run()`` returns early without calling ``target`` if its
        ``OwnedRuntimeThreads`` owner stops between ``start()`` and ``run()``.
        """
        dead_keys = [
            key
            for key, fl in self._inflight.items()
            if fl.worker is not None and not fl.worker.is_alive() and not fl.done.is_set()
        ]
        for key in dead_keys:
            fl = self._inflight.pop(key, None)
            if fl is not None:
                fl.done.set()

    def reset(self) -> None:
        """Clear all cached entries and detach any in-flight probes."""
        with self._lock:
            self._generation += 1
            self._entries.clear()
            stale_flights = list(self._inflight.values())
            self._inflight.clear()
        for flight in stale_flights:
            flight.done.set()

    def _put_entry_locked(self, scope_key: _ScopeKey, entry: _CatalogEntry) -> None:
        self._entries[scope_key] = entry
        self._entries.move_to_end(scope_key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def _record_timeout_locked(
        self,
        scope_key: _ScopeKey,
        existing: _CatalogEntry | None,
        ref_kind: str,
        ref: weakref.ReferenceType[object] | None,
        now: float,
    ) -> None:
        if (
            existing is not None
            and existing.probed_ok
            and now - existing.updated_at < self._stale_ttl_seconds
        ):
            updated = dataclasses.replace(existing, failed_at=now)
        else:
            updated = _CatalogEntry(
                exact_cf=frozenset(),
                prefixes_cf=frozenset(),
                probed_ok=False,
                updated_at=now,
                failed_at=now,
                ref_kind=ref_kind,
                ref=ref,
            )
        self._put_entry_locked(scope_key, updated)

    def _run_probe(
        self,
        scope_key: _ScopeKey,
        gw: object,
        ref_kind: str,
        ref: weakref.ReferenceType[object] | None,
        flight: _InFlightProbe,
    ) -> None:
        models: tuple[str, ...] = ()
        probed_ok = False
        try:
            models, probed_ok = _probe_gateway_models(gw)
        except Exception as exc:  # noqa: BLE001 - background probe must never raise
            _logger.debug(
                "sonder_inference background model probe failed: %s",
                type(exc).__name__,
            )
            models = ()
            probed_ok = False
        finally:
            finished_at = self._monotonic()
            entry = _build_catalog_entry(
                models,
                probed_ok=probed_ok,
                updated_at=finished_at,
                failed_at=None if probed_ok else finished_at,
                ref_kind=ref_kind,
                ref=ref,
            )
            with self._lock:
                flight.entry = entry
                if self._inflight.get(scope_key) is flight:
                    self._inflight.pop(scope_key, None)
                if flight.generation == self._generation:
                    self._put_entry_locked(scope_key, entry)
            flight.done.set()
            # Drop the strong reference only after inflight cleanup completes,
            # so ``id(gw)`` cannot be reused while ``flight`` is active.
            del gw

    def _launch_probe_thread(
        self,
        scope_key: _ScopeKey,
        gw: object,
        ref_kind: str,
        ref: weakref.ReferenceType[object] | None,
        flight: _InFlightProbe,
    ) -> bool:
        with self._lock:
            if flight.generation != self._generation:
                return False
        try:
            thread_factory = cast("Callable[..., threading.Thread]", owned_runtime_thread)
            worker = thread_factory(
                target=self._run_probe,
                args=(scope_key, gw, ref_kind, ref, flight),
                name="sonder-bridge-model-probe",
                daemon=True,
            )
            worker.start()
            with self._lock:
                flight.worker = worker
            return True
        except Exception as exc:  # noqa: BLE001 - thread startup failure degrades cleanly
            _logger.debug(
                "sonder_inference background model probe start refused: %s",
                type(exc).__name__,
            )
            now = self._monotonic()
            with self._lock:
                if self._inflight.get(scope_key) is flight:
                    self._inflight.pop(scope_key, None)
                if flight.generation == self._generation:
                    cur = self._entries.get(scope_key)
                    if cur is not None and not cur.matches_gateway(gw):
                        cur = None
                    self._record_timeout_locked(scope_key, cur, ref_kind, ref, now)
            flight.done.set()
            return False

    def advertises_model(self, gw: object, model_name: str) -> bool:
        wanted = str(model_name or "").strip()
        if not wanted or gw is None:
            return False
        scope_key = _gateway_probe_scope(gw)
        ref_kind, ref = _gateway_identity_ref(gw)

        start_bg: _InFlightProbe | None = None
        flight: _InFlightProbe | None = None
        start_new = False

        with self._lock:
            self._reap_dead_inflight_locked()
            now = self._monotonic()
            entry = self._entries.get(scope_key)
            if entry is not None and not entry.matches_gateway(gw):
                self._entries.pop(scope_key, None)
                entry = None

            if entry is not None:
                self._entries.move_to_end(scope_key)
                if entry.matches_model(wanted):
                    age = now - entry.updated_at
                    if age < self._positive_ttl_seconds:
                        return True
                    if age < self._stale_ttl_seconds:
                        in_cooldown = (
                            entry.failed_at is not None
                            and now - entry.failed_at < self._negative_ttl_seconds
                        )
                        if (
                            not in_cooldown
                            and scope_key not in self._inflight
                            and len(self._inflight) < self._max_inflight
                        ):
                            start_bg = _InFlightProbe(
                                self._generation,
                                now + self._wait_timeout_seconds,
                            )
                            self._inflight[scope_key] = start_bg
                    # Fall through past stale_ttl_seconds to bounded single-flight probe.
                else:
                    neg_anchor = (
                        entry.updated_at
                        if entry.failed_at is None
                        else max(entry.updated_at, entry.failed_at)
                    )
                    if now - neg_anchor < self._negative_ttl_seconds:
                        return False

            if start_bg is None and (
                entry is None
                or not entry.matches_model(wanted)
                or now - entry.updated_at >= self._stale_ttl_seconds
            ):
                existing_flight = self._inflight.get(scope_key)
                if existing_flight is not None:
                    if existing_flight.timed_out or existing_flight.deadline <= now:
                        existing_flight.timed_out = True
                        self._record_timeout_locked(scope_key, entry, ref_kind, ref, now)
                        return False
                    flight = existing_flight
                else:
                    if len(self._inflight) >= self._max_inflight:
                        self._record_timeout_locked(scope_key, entry, ref_kind, ref, now)
                        return False
                    flight = _InFlightProbe(
                        self._generation,
                        now + self._wait_timeout_seconds,
                    )
                    self._inflight[scope_key] = flight
                    start_new = True

        # Non-blocking background refresh for stale-while-revalidate positive hit.
        if flight is None:
            if start_bg is not None:
                self._launch_probe_thread(scope_key, gw, ref_kind, ref, start_bg)
            return True

        if start_new:
            if not self._launch_probe_thread(scope_key, gw, ref_kind, ref, flight):
                return False

        remaining = min(
            self._wait_timeout_seconds,
            max(0.0, flight.deadline - self._monotonic()),
        )
        flight.done.wait(timeout=remaining)

        with self._lock:
            if flight.done.is_set() and flight.entry is not None:
                return flight.entry.matches_model(wanted)
            if (
                flight.worker is not None
                and not flight.worker.is_alive()
                and not flight.done.is_set()
            ):
                if self._inflight.get(scope_key) is flight:
                    self._inflight.pop(scope_key, None)
                flight.done.set()
            flight.timed_out = True
            if flight.generation == self._generation:
                timeout_now = self._monotonic()
                cur = self._entries.get(scope_key)
                if cur is not None and not cur.matches_gateway(gw):
                    cur = None
                self._record_timeout_locked(scope_key, cur, ref_kind, ref, timeout_now)
            return False


_MODEL_ADVERTISEMENT_CACHE = _ModelAdvertisementCache()


def reset_model_advertisement_cache() -> None:
    """Clear the cached ``sonder_inference`` model advertisement probes (for tests/reloads)."""
    _MODEL_ADVERTISEMENT_CACHE.reset()


def provider_bindings(graph: object) -> object:
    """Bindings of the live graph when one exists, else the same env parse."""
    bindings = getattr(graph, "provider_bindings", None)
    if bindings is not None:
        return bindings
    from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env

    try:
        return provider_bindings_from_env()
    except ValueError as exc:
        # The gateway composition would refuse the same configuration; fail
        # the turn loudly rather than silently serving it from Ollama.
        raise ModelCallError("configuration", str(exc), status=503) from exc


def _find_sonder_inference_gateway(graph: object) -> object | None:
    """Locate the ``sonder_inference`` gateway (exposing ``unified_worker_status``)."""
    if graph is None:
        return None
    root_gw = getattr(graph, "model_gateway", None)
    if root_gw is None:
        return None
    seen: set[int] = set()
    queue: deque[object] = deque([root_gw])
    while queue:
        cand = queue.popleft()
        if cand is None or id(cand) in seen:
            continue
        seen.add(id(cand))
        if callable(getattr(cand, "unified_worker_status", None)):
            return cand
        for map_attr in ("providers", "_providers"):
            providers = getattr(cand, map_attr, None)
            if isinstance(providers, Mapping):
                preferred = providers.get("sonder_inference")
                if preferred is not None:
                    queue.appendleft(preferred)
                for key, value in providers.items():
                    if key != "sonder_inference":
                        queue.append(value)
        for attr in ("primary", "_primary", "fallback", "_fallback", "_gateway", "gateway"):
            nested = getattr(cand, attr, None)
            if nested is not None:
                queue.append(nested)
    return None


def _sonder_inference_advertises_model(graph: object, model_name: str) -> bool:
    """Return True when the graph's ``sonder_inference`` gateway advertises ``model_name``."""
    wanted = str(model_name or "").strip()
    if not wanted or graph is None:
        return False
    gw = _find_sonder_inference_gateway(graph)
    if gw is None:
        return False
    return _MODEL_ADVERTISEMENT_CACHE.advertises_model(gw, wanted)


def provider_for_tier(tier_label: object, cloud: bool, graph: object) -> str | None:
    """The provider serving a rung (Sonder Inference handles local, pins, and Ollama Cloud)."""
    bindings = provider_bindings(graph)
    if cloud:
        return "sonder_inference"
    provider = provider_bridge.provider_for_tier(tier_label, bindings)
    if provider_bridge.is_bridged(provider):
        return provider
    return "sonder_inference"


def endpoint_is_loopback(provider: str, graph: object) -> bool:
    """``provider_bridge``'s endpoint locality probe for the live graph.

    Asks the gateway the graph binds for ``provider`` (through tier dispatch
    and a pre-send fallback's primary).  Before a graph exists, the env
    configuration a default-built OpenAI-compatible gateway would read
    decides.  A gateway this cannot identify is not verified loopback.
    """
    from .provider_dispatch.fallback import PreSendFallbackGateway
    from .provider_dispatch.gateway import ProviderDispatchGateway

    gateway = getattr(graph, "model_gateway", None)
    if gateway is None:
        if provider != "openai_compatible":
            return False
        from .inference.openai_compat_gateway import configured_endpoint_is_loopback

        return configured_endpoint_is_loopback()
    bindings = getattr(graph, "provider_bindings", None)
    if bindings is not None and provider not in bindings.bound_providers:
        return False  # no gateway is bound for this provider
    seen: set[int] = set()
    while gateway is not None and id(gateway) not in seen:
        seen.add(id(gateway))
        if isinstance(gateway, ProviderDispatchGateway):
            gateway = gateway._providers.get(provider)
        elif isinstance(gateway, PreSendFallbackGateway):
            gateway = gateway.primary
        else:
            probe = getattr(gateway, "endpoint_is_loopback", None)
            return callable(probe) and probe() is True
    return False


def native_tools_supported(provider: str, graph: object) -> bool:
    """``provider_bridge``'s native-tool capability probe for the live graph.

    Fails closed without composing a graph when none exists yet or when no
    ready ``sonder_inference`` gateway advertises native tools.
    """
    if str(provider or "").strip().lower() != "sonder_inference":
        return False
    gateway = _find_sonder_inference_gateway(graph)
    if gateway is None:
        return False
    probe = getattr(gateway, "supports_native_tools", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Cancelled:
        raise
    except Exception:
        return False


def resolve_local_tier_health(
    tier_models: Mapping[str, str],
    *,
    graph: object = None,
    pool: object = None,
    now: float | None = None,
) -> dict[str, dict[str, object]]:
    """Build advisory per-tier health/latency hints from local runtime state.

    Zero network I/O: combines durable ``fanout_store`` model cooldowns,
    in-process ``activity_tracker`` latency/error measurements, and cached
    ``sonder_inference`` readiness snapshots when present.  Fails open (omits
    entries) rather than stalling or breaking chat turns.
    """
    del pool
    if not tier_models:
        return {}
    from .observability import activity_tracker
    from .persistence import fanout_store

    current_ts = time.time() if now is None else float(now)
    try:
        recent_metrics = activity_tracker.recent_model_metrics()
    except Exception:
        recent_metrics = {}
    cached_gateway_state = ""
    cached_gateway_reason = ""
    inf_gw = _find_sonder_inference_gateway(graph)
    if inf_gw is not None:
        cached_snap = getattr(inf_gw, "_health_cache", None)
        if cached_snap is not None and getattr(cached_snap, "state", "") == "unavailable":
            cached_gateway_state = "unavailable"
            cached_gateway_reason = str(getattr(cached_snap, "detail", "") or "sonder-inference unavailable")

    hints: dict[str, dict[str, object]] = {}
    for raw_tier, raw_model in tier_models.items():
        tier = str(raw_tier or "").strip().lower()
        model = str(raw_model or "").strip()
        if not tier or not model:
            continue
        available = True
        healthy = True
        latency_ms: float | None = None
        error_rate = 0.0
        reasons: list[str] = []

        try:
            stored = fanout_store.get_model_health(model)
        except Exception:
            stored = None
        if isinstance(stored, Mapping):
            disabled_until = stored.get("disabled_until")
            try:
                if disabled_until is not None and float(disabled_until) > current_ts:
                    available = False
                    healthy = False
                    reasons.append(str(stored.get("last_error") or "health cooldown active"))
            except (TypeError, ValueError):
                pass
            try:
                avail_failures = int(stored.get("availability_failure_count") or 0)
            except (TypeError, ValueError):
                avail_failures = 0
            if avail_failures >= 3 and healthy:
                healthy = False
                reasons.append(str(stored.get("last_error") or "repeated availability failures"))

        metric = recent_metrics.get(model) or recent_metrics.get(model[:120])
        if isinstance(metric, Mapping):
            raw_lat = metric.get("latency_ms")
            if isinstance(raw_lat, (int, float)) and not isinstance(raw_lat, bool):
                latency_ms = float(raw_lat)
            raw_err = metric.get("error_rate")
            if isinstance(raw_err, (int, float)) and not isinstance(raw_err, bool):
                error_rate = max(0.0, float(raw_err))

        if cached_gateway_state == "unavailable" and provider_for_tier(tier, False, graph) == "sonder_inference":
            available = False
            healthy = False
            reasons.append(cached_gateway_reason)

        entry: dict[str, object] = {
            "available": available,
            "healthy": healthy,
            "error_rate": error_rate,
        }
        if latency_ms is not None:
            entry["latency_ms"] = latency_ms
        clean_reason = "; ".join(part.strip() for part in reasons if part and part.strip())
        if clean_reason:
            entry["reason"] = clean_reason[:160]
        hints[tier] = entry
    return hints


def prompt_identity_model(model: str, tier_label: object, provider: str | None,
                          graph: object) -> str:
    """Only name a serving model when the bound provider cannot fall back."""
    if (provider == "sonder_inference"
            and provider_bindings(graph).fallbacks.get(provider) == "ollama"):
        # The same prompt may be sent to local Ollama with a different model.
        return ""
    from .inference.served_tier_models import served_prompt_model

    return served_prompt_model(model, tier_label, provider)


class BridgeCancellation:
    """Cancellation for one bridged model step: the legacy ``cancel_check`` only.

    The ambient HTTP context carries the lifecycle coordinator's token, which
    ``drain()`` cancels the moment a drain starts.  An admitted turn on the
    Ollama path is never interrupted by that token -- drain waits for it --
    so a bridged turn must not be either: honouring the coordinator token here
    would throw away an answer the provider already produced (and billed).
    Client disconnects and explicit cancels still arrive via ``cancel_check``.
    """

    def __init__(self, cancel_check: Callable[[], object] | None):
        self._check = cancel_check

    @property
    def cancelled(self) -> bool:
        return bool(self._check()) if callable(self._check) else False

    def wait(self, timeout: float | None = None) -> bool:
        if timeout:
            time.sleep(max(timeout, 0.0))
        return self.cancelled


def operation_context(
    timeout: float | None,
    cancel_check: Callable[[], object] | None,
    *,
    cloud_allowed: bool,
    remote_ollama_allowed: bool,
) -> OperationContext:
    """The ambient turn context (correlation id R), bounded by this call.

    The HTTP context's own 30 s default is an admission budget, not a model
    budget; the legacy call's timeout is the deadline, as on the Ollama path.
    Its cancellation is the legacy ``cancel_check`` only (see
    ``BridgeCancellation``): a drain that starts after admission lets the
    turn finish, exactly as it does for an Ollama rung.  Consent follows the
    same host policy ``_gateway_generate_text`` applies (injected by caller).
    """
    helper = provider_bridge.active_helper_context()
    if helper is not None:
        return helper
    deadline = time.monotonic() + float(timeout) if timeout else None
    ambient = current_operation_context()
    if ambient is not None:
        return dataclasses.replace(
            ambient,
            deadline_monotonic=deadline,
            cancellation=BridgeCancellation(cancel_check),
            cloud_allowed=cloud_allowed,
            remote_ollama_allowed=remote_ollama_allowed,
        )
    return local_owner_context(
        correlation_id="chat-%s" % os.urandom(6).hex(),
        timeout_seconds=float(timeout) if timeout else None,
        cancellation=BridgeCancellation(cancel_check),
        cloud_allowed=cloud_allowed,
        remote_ollama_allowed=remote_ollama_allowed,
    )


def _provider_display_url(gateway: object, provider: str) -> str:
    """Read safe endpoint metadata from the bound gateway, without probing it."""
    from .provider_dispatch.fallback import PreSendFallbackGateway
    from .provider_dispatch.gateway import ProviderDispatchGateway

    try:
        seen: set[int] = set()
        while id(gateway) not in seen:
            seen.add(id(gateway))
            if isinstance(gateway, ProviderDispatchGateway):
                gateway = gateway._providers.get(provider)
                continue
            if isinstance(gateway, PreSendFallbackGateway):
                # The binding describes the primary; a failed fallback already
                # retains both causes in the classified error's detail.
                gateway = gateway.primary
                continue
            settings = getattr(gateway, "settings", None)
            if not callable(settings):
                settings = getattr(gateway, "_resolved_config", None)
            if not callable(settings):
                break
            config = settings()
            display = getattr(config, "display_base_url", None)
            raw = display or getattr(config, "base_url", "")
            # Provider display properties already apply their path policy.
            # Generic configs have no such guarantee: show only their origin.
            resolved = _safe_url_origin(raw, include_path=bool(display))
            if not resolved:
                break
            return resolved
    except Exception:  # noqa: BLE001 - display metadata must not mask the provider failure
        pass
    return "(endpoint unavailable)"


def chat_request(
    gateway: object, payload: dict, rung, *, context: OperationContext, text_only: bool = False,
):
    """Serve one local chat step through the gateway for a non-Ollama rung.

    The model the provider reports it served is judged against the tool-lane
    deny list before the reply is returned (``tool_lane_guard.require_served``)
    on every call, text-only or not: a blocked model never serves a tier.  A
    text-only call keeps a reply that names no model.
    """
    from sonder_runtime.adapters.tool_lane_guard import require_served

    try:
        out, response = provider_bridge.generate_via_gateway(
            gateway, payload, tier=rung.tier, context=context,
        )
    except SonderError as exc:
        failure = provider_bridge.classify_failure(exc, provider=rung.provider)
        error = ModelCallError(
            failure.kind, failure.detail, status=failure.status,
            transient=failure.transient, attempts=1, cloud=False,
        )
        error.provider = rung.provider
        error.provider_display_url = _provider_display_url(gateway, rung.provider)
        raise error from exc
    require_served(rung, response.model, text_only=text_only)
    from sonder_runtime.application.agents.delegation_runtime import (
        current_board,
        current_depth,
        note_agent_execution,
    )
    origin = ""
    if current_depth() >= 1 or current_board() is not None:
        resolved = _provider_display_url(gateway, rung.provider)
        origin = "" if resolved == "(endpoint unavailable)" else resolved
    note_agent_execution(
        model=str(getattr(response, "model", "") or ""),
        origin=origin,
    )
    return out, response.text


def note_degradation(step: str, detail: str) -> None:
    """An Ollama-only step a non-Ollama rung ran without: loud, never silent."""
    _logger.warning("non-Ollama chat turn degraded: %s (%s)", step, detail)
    provider_bridge.record_degradation(step)


def ollama_only_reroute(
    gateway: object, payload: dict, rung, *, model: str, graph: object,
    loopback_endpoint: bool, context_probe: Callable[[], object],
) -> dict | None:
    """Local Ollama rerouting is retired; Sonder Inference carries all supported features."""
    provider = getattr(rung, "provider", None)
    if provider != "sonder_inference":
        return None
    found = provider_bridge.ollama_only_feature(payload, provider=provider)
    if found is None:
        return None
    _feature, message = found
    error = ModelCallError(
        provider_bridge.UNSUPPORTED_FEATURE_KIND, message,
        status=400, attempts=0, cloud=False,
    )
    error.provider = provider
    error.provider_display_url = _provider_display_url(gateway, provider)
    raise error


def reroute_may_use_pool(pool: object, *, base_loopback: bool, local_only: bool) -> bool:
    """Whether a pooled step may be placed by the worker pool."""
    return bool(
        base_loopback and not local_only
        and getattr(pool, "enabled", False) and getattr(pool, "has_remote_workers", False)
    )
