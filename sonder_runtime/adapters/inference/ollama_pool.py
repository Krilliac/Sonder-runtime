"""Bounded multi-host Ollama inference routing.

The pool is deliberately an inference transport, not a distributed model
runtime. Each worker owns its Ollama process and model files. The coordinator
performs bounded capability discovery, admits one request to one worker, and
can fail over only when that worker did not return a response. Model and
protocol errors are never replayed on another host.

Remote endpoints remain behind the independent Ollama consent and HTTPS
policy. Nothing in routing, health recovery, or capability probing can widen
that policy.
"""
from __future__ import annotations

from sonder_runtime.platform.runtime_threads import (
    ThreadPoolExecutor as owned_runtime_pool,
    run_bounded,
)

import base64
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
import contextvars
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import http.client
import importlib
import ipaddress
import itertools
import json
import math
import os
import re
import ssl
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit
import urllib.error
import urllib.request

import logging

import sonder_runtime.adapters.model_inventory as model_inventory
import sonder_runtime.adapters.inference.gpu_admission as gpu_admission_module
import sonder_runtime.adapters.inference.prewarm_gate as prewarm_gate
from sonder_runtime.domain import ollama_policy
from sonder_runtime.domain.inference_membership import (
    CapabilityEvidence, MembershipHighWater, MembershipReconciliation,
    MembershipRoster, MembershipSnapshot, WorkerAdvertisement,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.platform.logging import Redactor
from sonder_runtime.platform.metrics import MetricsRegistry, default_registry
from sonder_runtime.application.routing.identity_cache import IdentityObservationCache

logger = logging.getLogger(__name__)


_FAILOVER_HTTP_CODES = frozenset({502, 503, 504})
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_SECONDS = 30.0
_DEFAULT_MAX_INFLIGHT = 1
_DEFAULT_QUEUE_DEPTH = 32
_DEFAULT_ADMISSION_TIMEOUT_SECONDS = 1.0
# Agent/autopilot/fleet calls are background work.  They should be allowed to
# join the bounded pool queue long enough for another local request to finish,
# while the historical one-second interactive default remains unchanged.
_DEFAULT_LOCAL_ADMISSION_TIMEOUT_SECONDS = 30.0
_DEFAULT_CAPABILITY_TTL_SECONDS = 300.0
_DEFAULT_MAX_WORKERS = 16
_MAX_POOL_WORKERS = 256
_DEFAULT_CAPABILITY_PROBE_PARALLELISM = 4
_MAX_CAPABILITY_PROBE_PARALLELISM = 8
_DEFAULT_CAPABILITY_PROBE_BATCH_SIZE = 32
_MAX_CAPABILITY_PROBE_BATCH_SIZE = 128
_DEFAULT_STATUS_PAGE_SIZE = 32
_MAX_STATUS_PAGE_SIZE = 128
_MAX_STATUS_SERIALIZED_BYTES = 65_536
_STATUS_MODEL_PREVIEW_COUNT = 8
_STATUS_MODEL_PREVIEW_LENGTH = 128
_STATUS_SCHEMA_VERSION = 2
_MAX_MODELS_PER_WORKER = 2048
_MAX_INFLIGHT_PER_WORKER = 64
_MAX_QUEUE_DEPTH = 4096
_MAX_ADMISSION_TIMEOUT_SECONDS = 60.0
_LOCAL_CALLERS = frozenset({"agent", "autopilot", "fleet", "local"})
_LOCAL_ADMISSION_SCOPE = contextvars.ContextVar(
    "sonder_ollama_local_admission", default=None,
)
# ``(pool, lease)`` while ``OllamaWorkerPool.request`` runs a sender, so
# ``admitted()`` can attribute the GPU-admission phase to the held slot.
_ACTIVE_LEASE = contextvars.ContextVar("sonder_ollama_active_lease", default=None)
_DEFAULT_LEASE_WARN_AFTER_SECONDS = 120.0
_MAX_FAILURE_THRESHOLD = 100
_MAX_COOLDOWN_SECONDS = 3600.0
_MAX_CAPABILITY_TTL_SECONDS = 86_400.0
_PROBE_RESPONSE_LIMIT = 1_048_576
_PROTOCOL = "ollama-http-v1"
_MAX_METRIC_WORKERS = 16
_METRIC_OVERFLOW_LABEL = "overflow"
# A model miss may force one bounded capability batch at most this often, so a
# newly pulled model becomes routable without an operator refresh while a
# stream of requests for an absent model cannot turn into a probe storm.
_MODEL_MISS_REFRESH_INTERVAL_SECONDS = 30.0
_configured_workers: tuple[str, ...] | None = None
_configured_allow_remote: bool | None = None
_configured_trusted_origins: tuple[str, ...] | None = None
_configured_failure_threshold: int | None = None
_configured_cooldown_seconds: float | None = None
_configured_admission_timeout_ms: int | None = None
_configured_capability_ttl_seconds: int | None = None
_configured_probe_timeout_ms: int | None = None
_configured_max_inflight: int | None = None
_configured_queue_depth: int | None = None
_configured_max_workers: int | None = None
_configured_probe_parallelism: int | None = None
_configured_probe_batch_size: int | None = None
_configured_status_page_size: int | None = None
_configuration_lock = threading.RLock()
_configured_pool = None


class WorkerPoolError(urllib.error.URLError):
    """Base for privacy-safe pool admission and availability failures.

    ``retry_after_seconds`` is the pool's estimate of when to ask again, when
    it has one (a cooldown); ``pool_refusal_reply`` shows it to the user.
    """

    def __init__(self, reason, *, retry_after_seconds: float | None = None):
        super().__init__(reason)
        self.retry_after_seconds = retry_after_seconds


class CapabilityProbeDeferred(Exception):
    """GPU admission deferred a capability probe; the worker itself is not at fault."""


class WorkerPoolBackpressure(WorkerPoolError):
    """No worker capacity or bounded queue slot was available."""


class WorkerPoolDraining(WorkerPoolError):
    """New work was refused because pool drain has begun."""


class WorkerPoolUnavailable(WorkerPoolError):
    """No healthy worker can currently accept the request."""


class WorkerCapabilityUnavailable(WorkerPoolError):
    """Healthy workers do not advertise a required model/capability."""


def _is_loopback(origin: str) -> bool:
    host = urlsplit(origin).hostname
    if not host:
        return False
    if host.casefold().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_local_origin(origin: str) -> bool:
    """Return whether an origin is workstation/local-LAN inference."""
    host = urlsplit(origin).hostname
    if not host:
        return False
    if host.casefold().rstrip(".") == "localhost" or _is_loopback(origin):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_link_local


@contextmanager
def local_agent_admission(
    *, caller: str = "agent", timeout_seconds: float | None = None,
):
    """Mark nested model calls as bounded local agent work.

    Transport wrappers can use this scope without threading a pool-specific
    keyword through every helper.  The pool still decides whether its origins
    are local; hosted/public pools retain interactive fail-fast admission.
    ``timeout_seconds`` is the remaining model-call budget and is capped by
    the pool's configured local admission limit when the request is admitted.
    """
    normalized = str(caller or "").strip().casefold()
    if normalized not in _LOCAL_CALLERS:
        raise ValueError("caller must be agent, autopilot, fleet, or local")
    if timeout_seconds is not None:
        timeout_seconds = float(timeout_seconds)
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("timeout_seconds must be a finite value >= 0")
        timeout_seconds = min(timeout_seconds, _MAX_ADMISSION_TIMEOUT_SECONDS)
    token = _LOCAL_ADMISSION_SCOPE.set((normalized, timeout_seconds))
    try:
        yield
    finally:
        _LOCAL_ADMISSION_SCOPE.reset(token)


def _active_lease(pool, origin: str):
    """The lease ``pool.request`` holds on ``origin`` for this sender, if any."""
    active = _ACTIVE_LEASE.get()
    if active is None or active[0] is not pool:
        return None
    lease = active[1]
    return lease if lease.state.endpoint.origin == origin else None


@contextmanager
def admitted(pool, path: str, payload, origin: str, *, deadline: float | None = None):
    """Context manager that admits one request to ``origin`` through ``pool``.

    Every server-side call that reaches a local Ollama enters here: the pool's
    ``gpu_admission`` decides lane, queue place, ``num_ctx`` and host memory.
    A pool without that service (a test double) admits everything unchanged.
    The caller class comes from ``local_agent_admission`` when one is active.
    When ``pool.request`` holds a lease for this send, its phase records
    whether the slot is waiting in GPU admission or in the transport, and a
    request that queues in GPU admission returns its pool slot while it waits
    (so it cannot starve work that needs no GPU) and takes one back, ahead of
    fresh requests, once admitted.
    """
    service = getattr(pool, "gpu_admission", None)
    if service is None:
        with nullcontext(gpu_admission_module.passthrough_ticket(payload)) as ticket:
            yield ticket
        return
    scope = _LOCAL_ADMISSION_SCOPE.get()
    lease = _active_lease(pool, origin)
    extra = {}
    if lease is not None:
        lease.phase = "gpu_admission"
        if getattr(service, "accepts_queue_callback", False) is True:
            extra["on_queue"] = lambda: pool._park_lease(lease)
    entered = pool._clock() if lease is not None else 0.0
    with service.admit(
        path, payload, origin=origin, deadline=deadline,
        caller=scope[0] if scope else "interactive", prewarm=prewarm_gate.is_prewarm(),
        **extra,
    ) as ticket:
        if lease is not None:
            if not lease.counted:
                pool._unpark_lease(lease, deadline)
            lease.admission_seconds += max(0.0, pool._clock() - entered)
            lease.phase = "transport"
        yield ticket


@contextmanager
def admitted_get(pool, origin: str, path: str, *, timeout: float):
    """GET ``origin + path`` through ``pool.open_url`` inside one admission.

    Yields the open response; the admission (a PROBE slot for a loopback
    origin) is held until the caller has read it and leaves the block.
    """
    with admitted(pool, path, None, origin, deadline=time.monotonic() + timeout):
        with pool.open_url(urllib.request.Request(origin + path), timeout=timeout) as response:
            yield response


def _safe_error(error: BaseException) -> str:
    """Return bounded single-line diagnostics without response bodies."""
    if isinstance(error, gpu_admission_module.GpuAdmissionRefused):
        # A refusal is a ``URLError``; keep its typed reason, not "transport unavailable".
        reason = getattr(getattr(error, "refusal", None), "reason", None)
        return "GpuAdmissionRefused: %s" % _safe_scalar(
            getattr(reason, "value", reason), limit=64)
    if isinstance(error, urllib.error.HTTPError):
        return "HTTPError: HTTP %d" % int(error.code or 0)
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            return "URLError: transport timed out"
        if isinstance(reason, ssl.SSLCertVerificationError):
            return "URLError: TLS certificate verification failed"
        if isinstance(reason, ssl.SSLError):
            return "URLError: TLS handshake failed"
        return "URLError: transport unavailable"
    if isinstance(error, ssl.SSLCertVerificationError):
        return "SSLCertVerificationError: TLS certificate verification failed"
    text = str(getattr(error, "reason", error) or type(error).__name__)
    text = " ".join(text.replace("\x00", "").split())
    return "%s: %s" % (type(error).__name__, text[:200])


_DEFAULT_POOL_RETRY_SECONDS = 5


def pool_refusal_reply(error: BaseException) -> str | None:
    """The chat reply for a pool refusal, or ``None`` for any other error.

    A refusal means Sonder's own pool had no slot or worker for the request,
    not that Ollama is down, so it must not read as a connection failure.
    The ``ERROR contacting`` prefix keeps it a failed turn for telemetry.
    """
    if not isinstance(error, WorkerPoolError):
        return None
    hint = getattr(error, "retry_after_seconds", None)
    seconds = (
        max(1, math.ceil(hint)) if isinstance(hint, (int, float)) and math.isfinite(hint) and hint > 0
        else _DEFAULT_POOL_RETRY_SECONDS
    )
    if isinstance(error, WorkerPoolDraining):
        return "ERROR contacting Sonder's model pool: it is shutting down; retry in a few seconds."
    if isinstance(error, WorkerPoolBackpressure):
        return "ERROR contacting Sonder's model pool: it is busy; retry in %ds." % seconds
    if isinstance(error, WorkerPoolUnavailable):
        return (
            "ERROR contacting Sonder's model pool: no model worker is available right now; "
            "retry in %ds." % seconds
        )
    return None


def _safe_scalar(value: object, *, limit: int) -> str:
    return " ".join(str(value or "unknown").replace("\x00", "").split())[:limit]


def _positive_int(
    environment: Mapping[str, str],
    key: str,
    default: int,
    *,
    maximum: int,
) -> int:
    raw = str(environment.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError("%s must be an integer" % key) from error
    if value < 1:
        raise ValueError("%s must be >= 1" % key)
    if value > maximum:
        raise ValueError("%s must be <= %d" % (key, maximum))
    return value


def _bounded_seconds(
    environment: Mapping[str, str], key: str, default: float, *, maximum: float,
) -> float:
    """Read a bounded duration without accepting NaN, infinity, or negatives."""
    raw = str(environment.get(key, "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError("%s must be a number" % key) from error
    if not math.isfinite(value) or value < 0:
        raise ValueError("%s must be >= 0" % key)
    if value > maximum:
        raise ValueError("%s must be <= %g" % (key, maximum))
    return value


def parse_worker_origins(
    raw: str | None, *, max_workers: int = _DEFAULT_MAX_WORKERS,
) -> tuple[str, ...]:
    """Parse a comma/semicolon-separated worker origin list."""
    if not 1 <= max_workers <= _MAX_POOL_WORKERS:
        raise ValueError("max workers must be within 1..256")
    values = []
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip()
        if value:
            values.append(value)
    if len(values) > max_workers - 1:
        raise ValueError(
            "at most %d additional Ollama workers are supported" % (max_workers - 1)
        )
    normalized = tuple(ollama_policy.normalize(value) for value in values)
    if len(set(normalized)) != len(normalized):
        raise ValueError("worker origins contain a duplicate canonical origin")
    return normalized


def _model_key(name) -> str:
    text = str(name or "").strip().casefold()
    return text[:-7] if text.endswith(":latest") else text


def _metric_label(index: int) -> str:
    return "w%d" % index if index < _MAX_METRIC_WORKERS else _METRIC_OVERFLOW_LABEL


def validate_worker_origin(
    origin: str,
    *,
    allow_remote: bool,
    trusted_origins: tuple[str, ...] = (),
) -> str:
    """Normalize one worker origin under the Ollama trust policy."""
    # Decided on the text as given; normalize() never removes these characters.
    reason = ollama_policy.text_error(origin)
    if reason:
        raise ValueError("worker endpoint " + reason)
    normalized = ollama_policy.normalize(origin)
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("worker endpoint has an invalid port") from error
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("worker endpoint must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("worker endpoint must not contain inline credentials")
    if not parsed.hostname or port is None:
        raise ValueError("worker endpoint must include a host and explicit port")
    if "%" in parsed.hostname:
        raise ValueError("worker endpoint must not contain a zone id or percent escape")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(
            "worker endpoint must be an origin without a path, query, or fragment"
        )
    # Logged only once the value is a bare origin: a password with an unencoded
    # "/" parses as host and port with the rest in the path, rejected above.
    logger.debug(
        f"validating worker origin scheme={parsed.scheme!r}, host={parsed.hostname!r}, "
        f"port={port!r}, allow_remote={allow_remote}, trusted_origin_count={len(trusted_origins)}"
    )
    if not _is_loopback(normalized):
        logger.debug(f"origin {normalized!r} is remote, checking policy")
        if not allow_remote:
            raise ValueError(
                "remote worker endpoints require SONDER_ALLOW_REMOTE_OLLAMA=1"
            )
        if parsed.scheme.casefold() != "https":
            raise ValueError("remote worker endpoints must use https")
    result = normalized.rstrip("/")
    logger.debug(f"validated worker origin -> {result!r}")
    return result


def configure_typed_workers(
    worker_origins: tuple[str, ...],
    *,
    allow_remote: bool,
    trusted_origins: tuple[str, ...] = (),
    failure_threshold: int | None = None,
    cooldown_seconds: int | None = None,
    admission_timeout_ms: int | None = None,
    capability_ttl_seconds: int | None = None,
    probe_timeout_ms: int | None = None,
    max_inflight_per_worker: int | None = None,
    queue_depth: int | None = None,
    max_workers: int | None = None,
    capability_probe_parallelism: int | None = None,
    capability_probe_batch_size: int | None = None,
    status_page_size: int | None = None,
) -> None:
    logger.debug(f"configuring typed workers: count={len(worker_origins)}, allow_remote={allow_remote}, trusted_origin_count={len(trusted_origins)}")
    logger.info(f"configuring {len(worker_origins)} typed Ollama worker(s), allow_remote={allow_remote}")
    normalized = tuple(
        validate_worker_origin(
            origin,
            allow_remote=allow_remote,
            trusted_origins=trusted_origins,
        )
        for origin in tuple(worker_origins)
    )
    configured_max_workers = (
        _DEFAULT_MAX_WORKERS if max_workers is None else int(max_workers)
    )
    configured_probe_parallelism = (
        _DEFAULT_CAPABILITY_PROBE_PARALLELISM
        if capability_probe_parallelism is None
        else int(capability_probe_parallelism)
    )
    configured_probe_batch_size = (
        _DEFAULT_CAPABILITY_PROBE_BATCH_SIZE
        if capability_probe_batch_size is None
        else int(capability_probe_batch_size)
    )
    configured_status_page_size = (
        _DEFAULT_STATUS_PAGE_SIZE
        if status_page_size is None
        else int(status_page_size)
    )
    if not 1 <= configured_max_workers <= _MAX_POOL_WORKERS:
        raise ValueError("max workers must be within 1..256")
    if len(set(normalized)) != len(normalized):
        raise ValueError("worker origins contain a duplicate canonical origin")
    if len(normalized) > configured_max_workers - 1:
        raise ValueError(
            "at most %d additional Ollama workers are supported"
            % (configured_max_workers - 1)
        )
    if not 1 <= configured_probe_parallelism <= _MAX_CAPABILITY_PROBE_PARALLELISM:
        raise ValueError("capability probe parallelism must be within 1..8")
    if not 1 <= configured_probe_batch_size <= _MAX_CAPABILITY_PROBE_BATCH_SIZE:
        raise ValueError("capability probe batch size must be within 1..128")
    if not 1 <= configured_status_page_size <= _MAX_STATUS_PAGE_SIZE:
        raise ValueError("status page size must be within 1..128")
    global _configured_workers, _configured_allow_remote, _configured_trusted_origins
    global _configured_failure_threshold, _configured_cooldown_seconds
    global _configured_admission_timeout_ms, _configured_capability_ttl_seconds
    global _configured_probe_timeout_ms, _configured_max_inflight
    global _configured_queue_depth, _configured_max_workers
    global _configured_probe_parallelism, _configured_probe_batch_size
    global _configured_status_page_size, _configured_pool
    with _configuration_lock:
        _configured_pool = None
        _configured_workers = normalized
        _configured_allow_remote = allow_remote
        _configured_trusted_origins = trusted_origins
        _configured_failure_threshold = failure_threshold
        _configured_cooldown_seconds = cooldown_seconds
        _configured_admission_timeout_ms = admission_timeout_ms
        _configured_capability_ttl_seconds = capability_ttl_seconds
        _configured_probe_timeout_ms = probe_timeout_ms
        _configured_max_inflight = max_inflight_per_worker
        _configured_queue_depth = queue_depth
        _configured_max_workers = configured_max_workers
        _configured_probe_parallelism = configured_probe_parallelism
        _configured_probe_batch_size = configured_probe_batch_size
        _configured_status_page_size = configured_status_page_size


def reset_typed_workers() -> None:
    logger.info("typed Ollama worker configuration reset")
    global _configured_workers, _configured_allow_remote, _configured_trusted_origins
    global _configured_failure_threshold, _configured_cooldown_seconds
    global _configured_admission_timeout_ms, _configured_capability_ttl_seconds
    global _configured_probe_timeout_ms, _configured_max_inflight
    global _configured_queue_depth, _configured_max_workers
    global _configured_probe_parallelism, _configured_probe_batch_size
    global _configured_status_page_size, _configured_pool
    with _configuration_lock:
        _configured_pool = None
        _configured_workers = None
        _configured_allow_remote = None
        _configured_trusted_origins = None
        _configured_failure_threshold = None
        _configured_cooldown_seconds = None
        _configured_admission_timeout_ms = None
        _configured_capability_ttl_seconds = None
        _configured_probe_timeout_ms = None
        _configured_max_inflight = None
        _configured_queue_depth = None
        _configured_max_workers = None
        _configured_probe_parallelism = None
        _configured_probe_batch_size = None
        _configured_status_page_size = None


def has_configured_remote_workers(environment=None) -> bool:
    """Return whether typed or legacy worker configuration can leave localhost."""
    with _configuration_lock:
        typed_workers = _configured_workers
        typed_allow_remote = _configured_allow_remote
    if environment is None and typed_workers is not None and typed_allow_remote is not None:
        origins = typed_workers
    else:
        env = os.environ if environment is None else environment
        origins = parse_worker_origins(
            env.get("SONDER_OLLAMA_WORKERS"),
            max_workers=_positive_int(
                env,
                "SONDER_OLLAMA_POOL_MAX_WORKERS",
                _DEFAULT_MAX_WORKERS,
                maximum=_MAX_POOL_WORKERS,
            ),
        )
    return any(not _is_loopback(origin) for origin in origins)


@dataclass(frozen=True)
class WorkerEndpoint:
    origin: str
    worker_id: str
    # Retained for endpoint constructor compatibility; emitted metric labels
    # come from the process registry's immutable identity reservations.
    metric_label: str = "w0"


@dataclass(frozen=True)
class WorkerCapabilities:
    protocol: str
    version: str
    models: tuple[str, ...]
    observed_at: float
    effective_max_inflight: int
    model_contexts: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    loaded_models: tuple[str, ...] = ()
    vram_bytes: int | None = None
    ram_bytes: int | None = None
    kv_cache_type: str | None = None
    supports_tools: Mapping[str, bool] = field(default_factory=lambda: MappingProxyType({}))
    model_active_billions: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    model_metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: MappingProxyType({}))

    @staticmethod
    def _lookup_mapping(mapping: Mapping[str, Any], model: str | None):
        if not model or not mapping:
            return None
        if model in mapping:
            return mapping[model]
        target = _model_key(model)
        for candidate, value in mapping.items():
            if _model_key(candidate) == target:
                return value
        return None

    def context_for(self, model: str | None) -> int | None:
        value = self._lookup_mapping(self.model_contexts, model)
        return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None

    def model_supports_tools(self, model: str | None) -> bool | None:
        value = self._lookup_mapping(self.supports_tools, model)
        return value if isinstance(value, bool) else None

    def active_billions_for(self, model: str | None) -> float | None:
        value = self._lookup_mapping(self.model_active_billions, model)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            return float(value)
        return None


@dataclass(frozen=True)
class WorkerSnapshot:
    worker_id: str
    origin: str
    state: str
    healthy: bool
    inflight: int
    capacity: int
    consecutive_failures: int
    last_error: str
    cooldown_remaining_seconds: float
    latency_ewma_ms: float | None
    protocol: str
    version: str
    models: tuple[str, ...]
    capabilities_stale: bool
    cooldown_until: float = 0.0
    trips: int = 0
    probing: bool = False
    inference_ewma_ms: float | None = None
    model_contexts: dict[str, int] = field(default_factory=dict)
    loaded_models: tuple[str, ...] = ()
    vram_bytes: int | None = None
    ram_bytes: int | None = None
    kv_cache_type: str | None = None
    supports_tools: dict[str, bool] = field(default_factory=dict)
    model_active_billions: dict[str, float] = field(default_factory=dict)
    model_metadata: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def ewma_latency_ms(self):
        return self.latency_ewma_ms

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "origin": self.origin,
            "state": self.state,
            "healthy": self.healthy,
            "inflight": self.inflight,
            "capacity": self.capacity,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "cooldown_remaining_seconds": self.cooldown_remaining_seconds,
            "latency_ewma_ms": self.latency_ewma_ms,
            "protocol": self.protocol,
            "version": self.version,
            "models": list(self.models),
            "capabilities_stale": self.capabilities_stale,
            "cooldown_until": self.cooldown_until,
            "trips": self.trips,
            "probing": self.probing,
            "ewma_latency_ms": self.latency_ewma_ms or 0.0,
            "inference_ewma_ms": self.inference_ewma_ms,
            "model_contexts": dict(self.model_contexts),
            "loaded_models": list(self.loaded_models),
            "vram_bytes": self.vram_bytes,
            "ram_bytes": self.ram_bytes,
            "kv_cache_type": self.kv_cache_type,
            "supports_tools": dict(self.supports_tools),
            "model_active_billions": dict(self.model_active_billions),
            "model_metadata": dict(self.model_metadata),
        }


@dataclass
class _WorkerState:
    endpoint: WorkerEndpoint
    metric_label: str | None = None
    inflight: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    cooldown_until: float = 0.0
    half_open_inflight: bool = False
    # Lease id of the request holding the half-open probe (``None`` for a bare
    # ``_acquire`` or none); only that lease's end, or a success, ends it.
    half_open_owner: int | None = None
    # Control-plane RTT: capability probes and model-less requests. Ranks
    # model-less reads, and inference until every candidate has an
    # ``inference_ewma_ms`` (the two are never compared with each other).
    latency_ewma_ms: float | None = None
    # Model-pinned request time, excluding time spent in GPU admission.
    inference_ewma_ms: float | None = None
    capabilities: WorkerCapabilities | None = None
    compatibility_error: str = ""
    capability_probe_failed: bool = False
    capability_probe_inflight: bool = False
    capability_probe_generation: int = 0
    trips: int = 0
    known_models: frozenset[str] | None = None
    advertisement: WorkerAdvertisement | None = None
    membership_state: str | None = None
    membership_expires_at: datetime | None = None
    membership_evidence: CapabilityEvidence | None = None
    capability_checked_at: datetime | None = None
    # Requests returning from a GPU-admission queue that wait for a slot here;
    # fresh requests do not take this worker while any wait (``_choose``).
    unpark_waiters: int = 0


@dataclass(eq=False)
class _PoolLease:
    """One request's hold on one worker slot, from ``_acquire`` to its close.

    Owned by the pool that opened it and mutated under that pool's
    ``_condition``; ``phase`` alone is a single attribute store made by the
    owning request thread for diagnostics. While ``counted`` the lease is one
    unit of ``state.inflight``; ``closed`` is set once, by the first close.
    """

    id: int
    state: _WorkerState
    worker_id: str
    model: str
    caller: str
    thread: threading.Thread
    started: float
    phase: str = "dispatching"  # dispatching | gpu_admission | transport
    counted: bool = True
    closed: bool = False
    warned: bool = False
    # Pool-clock seconds this request spent inside GPU admission (queue and
    # unpark wait); excluded from the worker's inference latency.
    admission_seconds: float = 0.0

    @property
    def half_open(self) -> bool:
        """Whether this lease holds its worker's half-open probe."""
        return self.state.half_open_inflight and self.state.half_open_owner == self.id


def _worker_id(origin: str) -> str:
    parsed = urlsplit(origin)
    host = parsed.hostname or "worker"
    port = parsed.port or 443
    return "%s:%s" % (host, port)


def _model_names(payload) -> tuple[str, ...]:
    names = []
    for row in (payload or {}).get("models") or ():
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or row.get("model") or "").strip()
        if name and len(name) <= 256:
            names.append(name)
        if len(names) >= _MAX_MODELS_PER_WORKER:
            break
    return tuple(sorted(set(names)))


_SHOW_CACHE_LIMIT = 512
_SHOW_FETCHES_PER_PROBE = 8
_KV_CACHE_TYPES = frozenset({"f32", "f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0"})


def _positive_int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            parsed = int(text)
            return parsed if parsed > 0 else None
    return None


def _parse_billions(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if not math.isfinite(num) or num <= 0:
            return None
        return round(num / 1e9, 3) if num >= 1_000_000 else round(num, 3)
    text = str(value).strip().upper()
    if not text:
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)\s*([BM]?)$", text)
    if not match:
        return None
    num = float(match.group(1))
    unit = match.group(2)
    if not math.isfinite(num) or num <= 0:
        return None
    if unit == "M":
        return round(num / 1000.0, 3)
    return round(num, 3)


def _parse_moe_active_billions(
    model_name: str,
    details: Mapping[str, Any] | None,
    model_info: Mapping[str, Any] | None,
) -> float | None:
    if isinstance(model_info, Mapping):
        for key, raw in model_info.items():
            lowered = str(key).casefold()
            if lowered.endswith((".active_parameters", ".active_parameter_count", ".active_billions")):
                parsed = _parse_billions(raw)
                if parsed is not None:
                    return parsed
    if isinstance(details, Mapping):
        parsed = _parse_billions(details.get("active_parameter_size"))
        if parsed is not None:
            return parsed
    moe_tag = re.search(r"(?:^|[:\-_])a(\d+(?:\.\d+)?)b(?=$|[:\-_])", str(model_name or ""), re.IGNORECASE)
    if moe_tag:
        parsed = _parse_billions(moe_tag.group(1))
        if parsed is not None:
            return parsed
    if isinstance(model_info, Mapping) and isinstance(details, Mapping):
        expert_count = None
        expert_used = None
        for key, raw in model_info.items():
            lowered = str(key).casefold()
            if lowered.endswith(".expert_count"):
                expert_count = _positive_int_value(raw)
            elif lowered.endswith(".expert_used_count"):
                expert_used = _positive_int_value(raw)
        total = _parse_billions(details.get("parameter_size"))
        if expert_count and expert_used and expert_count > 1 and total:
            return round(max(0.5, total * (expert_used / expert_count)), 3)
    return None


def _parse_show_context_length(show_payload: Mapping[str, Any]) -> int | None:
    model_info = show_payload.get("model_info")
    if isinstance(model_info, Mapping):
        for key, raw in model_info.items():
            if str(key).casefold().endswith(".context_length"):
                parsed = _positive_int_value(raw)
                if parsed is not None:
                    return parsed
    parameters = show_payload.get("parameters")
    if isinstance(parameters, str):
        for line in parameters.splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0].casefold() == "num_ctx":
                parsed = _positive_int_value(parts[1])
                if parsed is not None:
                    return parsed
    return None


def _extract_show_metadata(
    model_name: str,
    tag_entry: Mapping[str, Any] | None,
    show_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    details = {}
    if isinstance(tag_entry, Mapping) and isinstance(tag_entry.get("details"), Mapping):
        details.update(tag_entry["details"])
    if isinstance(show_payload, Mapping) and isinstance(show_payload.get("details"), Mapping):
        details.update(show_payload["details"])
    model_info = show_payload.get("model_info") if isinstance(show_payload, Mapping) else None
    capabilities_raw = show_payload.get("capabilities") if isinstance(show_payload, Mapping) else None
    capabilities = tuple(
        sorted({str(item).strip().casefold() for item in capabilities_raw if str(item).strip()})
    ) if isinstance(capabilities_raw, (list, tuple, set, frozenset)) else ()
    supports_tools: bool | None = ("tools" in capabilities) if capabilities else None
    context_length = _parse_show_context_length(show_payload) if isinstance(show_payload, Mapping) else None
    parameter_billions = _parse_billions(details.get("parameter_size"))
    active_billions = _parse_moe_active_billions(model_name, details, model_info)
    meta: dict[str, Any] = {}
    if context_length is not None:
        meta["context_length"] = context_length
    if supports_tools is not None:
        meta["supports_tools"] = supports_tools
    if capabilities:
        meta["capabilities"] = list(capabilities)
    if parameter_billions is not None:
        meta["parameter_billions"] = parameter_billions
    if active_billions is not None:
        meta["active_billions"] = active_billions
    digest = str(tag_entry.get("digest") or "").strip() if isinstance(tag_entry, Mapping) else ""
    if digest:
        meta["digest"] = digest[:96]
    return meta


def _default_capability_prober(*, allow_remote: bool, timeout: float = 2.0, open_url=None):
    """Build a bounded prober using the same no-proxy/no-redirect transport.

    Reads ``/api/version``, ``/api/tags``, best-effort ``/api/ps``, and lazy
    per-``(origin, model, digest)`` cached ``/api/show`` metadata without
    failing if optional endpoints are unavailable.
    """
    ollama_endpoint = importlib.import_module(
        "sonder_runtime.adapters.inference.ollama_endpoint"
    )
    show_cache: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
    show_lock = threading.Lock()

    def read(
        origin: str,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
    ) -> dict:
        transport = open_url or ollama_endpoint.open_url
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        request = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
        with transport(
            request, timeout=timeout, allow_remote=allow_remote,
        ) as response:
            raw = response.read(_PROBE_RESPONSE_LIMIT + 1)
        if not isinstance(raw, bytes):
            raise ValueError("capability response is not bytes")
        if len(raw) > _PROBE_RESPONSE_LIMIT:
            raise ValueError("capability response exceeded 1 MiB")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("capability response must be an object")
        return payload

    def read_show_cached(
        origin: str,
        model_name: str,
        *,
        digest: str = "",
        tag_entry: Mapping[str, Any] | None = None,
        allow_fetch: bool = True,
    ) -> dict[str, Any]:
        cache_key = (origin, model_name, digest)
        with show_lock:
            cached = show_cache.get(cache_key)
            if cached is not None:
                show_cache.move_to_end(cache_key)
                return dict(cached)
        if not allow_fetch:
            return _extract_show_metadata(model_name, tag_entry, None)
        show_payload: dict | None = None
        try:
            show_payload = read(origin, "/api/show", method="POST", body={"model": model_name})
        except Exception:
            show_payload = None
        meta = _extract_show_metadata(model_name, tag_entry, show_payload)
        if show_payload is not None:
            with show_lock:
                show_cache[cache_key] = dict(meta)
                show_cache.move_to_end(cache_key)
                while len(show_cache) > _SHOW_CACHE_LIMIT:
                    show_cache.popitem(last=False)
        return meta

    def probe(origin: str) -> dict:
        try:
            version = read(origin, "/api/version")
        except urllib.error.HTTPError as error:
            # Older compatible Ollama builds may lack the informational
            # version route. Model inventory is the load-bearing capability.
            if int(error.code or 0) != 404:
                raise
            version = {"version": "unknown"}
        tags = read(origin, "/api/tags")
        models = _model_names(tags)
        tag_entries: dict[str, Mapping[str, Any]] = {}
        raw_tags = tags.get("models") if isinstance(tags, Mapping) else None
        if isinstance(raw_tags, list):
            for entry in raw_tags:
                if isinstance(entry, Mapping):
                    name = str(entry.get("name") or entry.get("model") or "").strip()
                    if name and name not in tag_entries:
                        tag_entries[name] = entry

        ps_payload: Mapping[str, Any] | None = None
        try:
            ps_payload = read(origin, "/api/ps")
        except Exception:
            ps_payload = None

        loaded_models: list[str] = []
        ps_contexts: dict[str, int] = {}
        if isinstance(ps_payload, Mapping):
            ps_rows = ps_payload.get("models")
            if isinstance(ps_rows, list):
                for row in ps_rows:
                    if not isinstance(row, Mapping):
                        continue
                    name = str(row.get("name") or row.get("model") or "").strip()[:256]
                    if name:
                        loaded_models.append(name)
                        ctx = _positive_int_value(row.get("context_length"))
                        if ctx is not None:
                            ps_contexts[name] = ctx

        model_contexts: dict[str, int] = dict(ps_contexts)
        supports_tools: dict[str, bool] = {}
        model_active_billions: dict[str, float] = {}
        model_metadata: dict[str, dict[str, Any]] = {}
        for name in models:
            tag_entry = tag_entries.get(name)
            digest = str(tag_entry.get("digest") or "").strip() if tag_entry else ""
            # Lazy-cached /api/show: probe(origin) reads cached show metadata and
            # /api/tags details without firing synchronous /api/show requests during
            # bulk inventory probes; callers trigger show(origin, model) lazily.
            meta = read_show_cached(
                origin,
                name,
                digest=digest,
                tag_entry=tag_entry,
                allow_fetch=False,
            )
            if meta:
                model_metadata[name] = meta
            if isinstance(meta.get("context_length"), int):
                model_contexts[name] = meta["context_length"]
            if isinstance(meta.get("supports_tools"), bool):
                supports_tools[name] = meta["supports_tools"]
            if isinstance(meta.get("active_billions"), (int, float)):
                model_active_billions[name] = float(meta["active_billions"])

        kv_type_raw = None
        vram_raw = None
        ram_raw = None
        for source in (ps_payload, version):
            if not isinstance(source, Mapping):
                continue
            if kv_type_raw is None:
                candidate_kv = str(source.get("kv_cache_type") or "").strip().lower()
                if candidate_kv in _KV_CACHE_TYPES:
                    kv_type_raw = candidate_kv
            if vram_raw is None:
                vram_raw = _positive_int_value(
                    source.get("vram_bytes") or source.get("gpu_vram_bytes")
                )
            if ram_raw is None:
                ram_raw = _positive_int_value(
                    source.get("ram_bytes") or source.get("host_ram_bytes")
                )

        report: dict[str, Any] = {
            "protocol": _PROTOCOL,
            "version": str(version.get("version") or "unknown")[:80],
            "models": models,
        }
        if model_contexts:
            report["model_contexts"] = model_contexts
        if loaded_models:
            report["loaded_models"] = tuple(sorted(set(loaded_models)))
        if vram_raw is not None:
            report["vram_bytes"] = vram_raw
        if ram_raw is not None:
            report["ram_bytes"] = ram_raw
        if kv_type_raw is not None:
            report["kv_cache_type"] = kv_type_raw
        if supports_tools:
            report["supports_tools"] = supports_tools
        if model_active_billions:
            report["model_active_billions"] = model_active_billions
        if model_metadata:
            report["model_metadata"] = model_metadata
        return report

    def show(origin: str, model_name: str, *, digest: str = "") -> dict[str, Any]:
        return read_show_cached(origin, model_name, digest=digest, allow_fetch=True)

    def clear_show_cache() -> None:
        with show_lock:
            show_cache.clear()

    probe.show = show  # type: ignore[attr-defined]
    probe.clear_show_cache = clear_show_cache  # type: ignore[attr-defined]
    probe._sonder_default_allow_remote = allow_remote  # type: ignore[attr-defined]
    return probe


def _bounded_capability_probe(prober, origin, timeout, on_complete):
    """Run one probe without allowing an uncooperative callable to hold the pool.

    The transport prober has its own socket timeout, but injected probers and
    broken transports can still fail to return.  A daemon boundary lets the
    pool classify that case as a timeout and keep its owned executor
    cancellable.  Late results are intentionally discarded by the caller;
    ``on_complete`` only releases the in-flight marker.
    """
    value, error, _completed = run_bounded(
        lambda: prober(origin),
        max(0.001, float(timeout)),
        on_complete=on_complete,
        name="sonder-ollama-capability-probe",
    )
    return value, error


class OllamaWorkerPool:
    """Thread-safe, model-aware scheduler for independent Ollama hosts."""

    def __init__(
        self,
        primary_origin: str,
        worker_origins: tuple[str, ...] = (),
        *,
        allow_remote: bool = False,
        trusted_origins: tuple[str, ...] = (),
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        max_inflight_per_worker: int = _DEFAULT_MAX_INFLIGHT,
        queue_depth: int = _DEFAULT_QUEUE_DEPTH,
        admission_timeout_seconds: float = _DEFAULT_ADMISSION_TIMEOUT_SECONDS,
        local_admission_timeout_seconds: float = _DEFAULT_LOCAL_ADMISSION_TIMEOUT_SECONDS,
        capability_ttl_seconds: float = _DEFAULT_CAPABILITY_TTL_SECONDS,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        capability_probe_parallelism: int = _DEFAULT_CAPABILITY_PROBE_PARALLELISM,
        capability_probe_batch_size: int = _DEFAULT_CAPABILITY_PROBE_BATCH_SIZE,
        capability_probe_timeout_seconds: float = 2.0,
        status_page_size: int = _DEFAULT_STATUS_PAGE_SIZE,
        lease_warn_after_seconds: float = _DEFAULT_LEASE_WARN_AFTER_SECONDS,
        gpu_admission=None,
        capability_prober: Callable[[str], object] | None = None,
        recent_evidence=None,
        identity_for=None,
        capability_routing: str = "advisory",
        clock: Callable[[], float] = time.monotonic,
        time_fn: Callable[[], float] | None = None,
        metrics: MetricsRegistry | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        if not 1 <= failure_threshold <= _MAX_FAILURE_THRESHOLD:
            raise ValueError("failure threshold must be within 1..100")
        if not 1 <= cooldown_seconds <= _MAX_COOLDOWN_SECONDS:
            raise ValueError("cooldown seconds must be within 1..3600")
        if not 1 <= max_inflight_per_worker <= _MAX_INFLIGHT_PER_WORKER:
            raise ValueError("max inflight per worker must be within 1..64")
        if not 0 <= queue_depth <= _MAX_QUEUE_DEPTH:
            raise ValueError("queue depth must be within 0..4096")
        if not 0 <= admission_timeout_seconds <= _MAX_ADMISSION_TIMEOUT_SECONDS:
            raise ValueError("admission timeout must be within 0..60 seconds")
        if not 0 <= local_admission_timeout_seconds <= _MAX_ADMISSION_TIMEOUT_SECONDS:
            raise ValueError("local admission timeout must be within 0..60 seconds")
        if not 1 <= capability_ttl_seconds <= _MAX_CAPABILITY_TTL_SECONDS:
            raise ValueError("capability TTL must be within 1..86400 seconds")
        if not 1 <= max_workers <= _MAX_POOL_WORKERS:
            raise ValueError("max workers must be within 1..256")
        if not 1 <= capability_probe_parallelism <= _MAX_CAPABILITY_PROBE_PARALLELISM:
            raise ValueError("capability probe parallelism must be within 1..8")
        if not 1 <= capability_probe_batch_size <= _MAX_CAPABILITY_PROBE_BATCH_SIZE:
            raise ValueError("capability probe batch size must be within 1..128")
        if not 0 < capability_probe_timeout_seconds <= 30.0:
            raise ValueError("capability probe timeout must be within (0, 30] seconds")
        if not 1 <= status_page_size <= _MAX_STATUS_PAGE_SIZE:
            raise ValueError("status page size must be within 1..128")
        lease_warn_after_seconds = float(lease_warn_after_seconds)
        if not (math.isfinite(lease_warn_after_seconds) and lease_warn_after_seconds > 0):
            raise ValueError("lease warn-after must be a finite value > 0 seconds")
        capability_routing = str(capability_routing or "advisory").strip().casefold()
        if capability_routing not in {"advisory", "strict", "off"}:
            raise ValueError("capability routing must be advisory, strict, or off")
        all_origins = (primary_origin, *worker_origins)
        normalized_origins = []
        seen = set()
        primary_normalized = ""
        for index, raw in enumerate(all_origins):
            origin = validate_worker_origin(raw, allow_remote=allow_remote, trusted_origins=trusted_origins)
            if origin in seen:
                if index and origin == primary_normalized:
                    raise ValueError(
                        "worker origin duplicates primary after canonical normalization"
                    )
                raise ValueError("worker origins contain a duplicate canonical origin")
            seen.add(origin)
            normalized_origins.append(origin)
            if index == 0:
                primary_normalized = origin
        if len(normalized_origins) > max_workers:
            raise ValueError("at most %d Ollama workers are supported" % max_workers)
        states = [
            _WorkerState(
                WorkerEndpoint(origin, _worker_id(origin), _metric_label(index))
            )
            for index, origin in enumerate(normalized_origins)
        ]
        if not states:
            raise ValueError("at least one Ollama worker is required")
        logger.debug(
            f"OllamaWorkerPool.__init__: workers={len(states)}, "
            f"origins={[s.endpoint.origin for s in states]}, "
            f"failure_threshold={failure_threshold}, cooldown={cooldown_seconds}s, "
            f"max_inflight={max_inflight_per_worker}, queue_depth={queue_depth}, "
            f"admission_timeout={admission_timeout_seconds}s, capability_ttl={capability_ttl_seconds}s, "
            f"local_admission_timeout={local_admission_timeout_seconds}s, "
            f"max_workers={max_workers}, probe_parallelism={capability_probe_parallelism}, "
            f"probe_batch_size={capability_probe_batch_size}, status_page_size={status_page_size}"
        )
        logger.info(
            f"Ollama worker pool initialized with {len(states)} worker(s), "
            f"max_inflight={max_inflight_per_worker}, queue_depth={queue_depth}"
        )
        # Startup authority is independent of the live, drainable roster.
        self._allow_remote = bool(allow_remote)
        self._trusted_origins = tuple(trusted_origins)
        self._configured_origins = tuple(normalized_origins)
        self._local_origins = all(_is_local_origin(origin) for origin in normalized_origins)
        self._states = states
        self._failure_threshold = int(failure_threshold)
        self._cooldown_seconds = float(cooldown_seconds)
        self._max_inflight = int(max_inflight_per_worker)
        self._queue_depth = int(queue_depth)
        self._admission_timeout = float(admission_timeout_seconds)
        self._local_admission_timeout = float(local_admission_timeout_seconds)
        self._capability_ttl = float(capability_ttl_seconds)
        self._max_workers = int(max_workers)
        self._probe_parallelism = int(capability_probe_parallelism)
        self._probe_batch_size = int(capability_probe_batch_size)
        self._probe_timeout = float(capability_probe_timeout_seconds)
        self._status_page_size = int(status_page_size)
        self._capability_prober = capability_prober
        self.gpu_admission = (
            gpu_admission if gpu_admission is not None else gpu_admission_module.process_service()
        )
        self._recent_evidence = recent_evidence
        self._identity_for = identity_for
        self._capability_routing = capability_routing
        self._clock = time_fn or clock
        self._cursor = 0
        self._probe_cursor = 0
        self._roster_generation = 1
        self._status_cursor_key = os.urandom(32)
        self._waiters = 0
        self._draining = False
        self._condition = threading.Condition(threading.RLock())
        # Open slot leases by id; guarded by ``_condition`` (see ``_PoolLease``).
        self._leases: dict[int, _PoolLease] = {}
        self._lease_ids = itertools.count(1)
        self._leases_reclaimed = 0
        self._lease_warn_after = lease_warn_after_seconds
        self._probe_lock = threading.Lock()
        self._identity_cache = IdentityObservationCache(clock=self._clock)
        self._metrics = {
            "logical_requests": 0,
            "dispatches": 0,
            "failovers": 0,
            "transport_failures": 0,
            "backpressure_rejections": 0,
            "drain_rejections": 0,
            "capability_probes": 0,
            "capability_probe_failures": 0,
            "reconnects": 0,
            "model_miss_refreshes": 0,
        }
        self._metrics_observer = metrics
        # Production pools share their process registry. Lightweight injected
        # observers still get a bounded allocator for the lifetime of this pool.
        self._metric_label_registry = (
            metrics if callable(getattr(metrics, "reserve_ollama_worker_label", None))
            else MetricsRegistry(enabled=False)
        )
        self._redactor = redactor or Redactor()
        self._membership_clock = None
        self._membership_authority = None
        self._membership_high_water = None
        self._membership_roster_applied = False
        self._configured_remote_origins = frozenset(
            origin for origin in normalized_origins if not _is_loopback(origin))
        self._local_worker_count = len(states) - len(self._configured_remote_origins)
        self._membership_omitted = 0
        self._external_source = None
        self._request_wave_inflight = False
        self._last_model_miss_refresh = None

    @staticmethod
    def _identity_cache_key(origin, model, payload):
        try:
            from .capability_evidence import identity_context_tokens
            context_tokens = identity_context_tokens(payload)
        except (AttributeError, TypeError, ValueError, OSError):
            options = payload.get("options") if isinstance(payload, Mapping) else None
            if not isinstance(options, Mapping):
                options = {}
            context_tokens = options.get(
                "num_ctx", payload.get("num_ctx") if isinstance(payload, Mapping) else None,
            )
        return (origin, model, context_tokens)

    def _observe_identity_cached(self, origin, model, payload):
        """Resolve worker identity once per evidence revision and 60s window."""
        if self._identity_for is None:
            return None
        key = self._identity_cache_key(origin, model, payload)
        return self._identity_cache.observe(
            key,
            lambda: self._identity_for(origin, model, payload),
            evidence=self._recent_evidence,
        )

    @property
    def membership_limit(self) -> int:
        return max(1, self._max_workers - self._local_worker_count)

    def configure_membership(self, *, cluster_id, issuer_id, clock) -> None:
        # Validate primitive authority fields before changing admission state.
        MembershipHighWater(cluster_id, issuer_id, 1, "0" * 64)
        with self._condition:
            if self._membership_clock is not None:
                raise ValueError("membership controller is already configured")
            if any(state.inflight and not _is_loopback(state.endpoint.origin) for state in self._states):
                raise ValueError("cannot attach membership while remote work is in flight")
            self._membership_clock = clock
            self._membership_authority = (cluster_id, issuer_id)
            for state in self._states:
                if not _is_loopback(state.endpoint.origin):
                    state.membership_state = "probation"
                    state.capabilities = None
            self._roster_generation += 1

    def configure_external_source(self, source, *, probe_timeout_seconds=2.0) -> None:
        from .external_membership import ExternalMembershipSource
        if type(source) is not ExternalMembershipSource or self._membership_clock is not None:
            raise ValueError("exact external source required before membership composition")
        allowed = frozenset(policy.origin for policy in source._config.member_policies)
        if not self._configured_remote_origins <= allowed:
            raise ValueError("configured remote worker lacks external endpoint policy")
        self._external_source = source
        self._configured_remote_origins = allowed
        self._capability_prober = _default_capability_prober(
            allow_remote=True, timeout=probe_timeout_seconds, open_url=self.open_url)

    def open_url(self, request, *, timeout, allow_remote=None):
        import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint
        if self._external_source is not None:
            if not _is_loopback(request.full_url):
                return self._external_source.open_worker_url(request, timeout=timeout)
            origin = ollama_policy.normalize(ollama_endpoint._origin_from_url(request.full_url))
            if (not self._external_source._config.local_fallback
                    or origin not in self.configured_origins):
                raise WorkerPoolUnavailable("external membership does not authorize local fallback")
        return ollama_endpoint.open_url(request, timeout=timeout, allow_remote=allow_remote)

    def validate_membership_snapshot(self, snapshot: MembershipSnapshot) -> None:
        if type(snapshot) is not MembershipSnapshot or self._membership_authority is None:
            raise ValueError("verified configured membership snapshot required")
        if (snapshot.cluster_id, snapshot.issuer_id) != self._membership_authority:
            raise ValueError("membership authority differs from static configuration")
        limit = (self._external_source._config.snapshot_max_advertisements
                 if self._external_source is not None else self.membership_limit)
        if len(snapshot.workers) > limit:
            raise ValueError("membership exceeds configured remote roster bound")
        if any(worker.origin not in self._configured_remote_origins for worker in snapshot.workers):
            raise ValueError("membership origin is not an exact configured remote origin")

    def apply_membership(self, result: MembershipReconciliation) -> None:
        """Atomically publish a validated roster; retain original draining states.

        Live and draining states together never exceed the configured limit.
        When drains occupy all slots, new members wait for a later refresh.
        Endpoint objects attached to in-flight work are never rewritten.
        """
        if type(result) is not MembershipReconciliation or self._membership_authority is None:
            raise ValueError("exact configured membership reconciliation required")
        if result.roster is not None:
            self.validate_membership_snapshot(result.roster.snapshot)
        water = result.high_water
        if water is not None and (water.cluster_id, water.issuer_id) != self._membership_authority:
            raise ValueError("membership high-water authority mismatch")
        with self._condition:
            old_water = self._membership_high_water
            if old_water is not None and (water is None or water.generation < old_water.generation
                    or (water.generation == old_water.generation and water.digest != old_water.digest)):
                raise ValueError("membership high-water cannot roll back or conflict")
            self._membership_high_water = water
            members = result.roster.members if result.roster is not None else ()
            desired = {(member.advertisement.worker_id, member.advertisement.origin,
                        member.advertisement.member_generation): member for member in members}
            new_admissions = {(worker.worker_id, worker.origin, worker.member_generation)
                              for worker in result.additions}
            retained = []
            existing = {}
            unresolved_by_origin = {}
            for state in self._states:
                if state.membership_state is None:
                    retained.append(state)
                    continue
                advertisement = state.advertisement
                key = ((advertisement.worker_id, advertisement.origin, advertisement.member_generation)
                       if advertisement is not None else None)
                if key in desired and state.membership_state != "draining":
                    existing[key] = state
                else:
                    state.membership_state = "draining"
                    if self._keeps_draining_state_locked(state):
                        retained.append(state)
                    if state.capability_probe_inflight:
                        unresolved_by_origin[state.endpoint.origin] = state
            omitted = result.omitted_worker_count
            for key, member in desired.items():
                state = existing.get(key)
                if state is None:
                    # A lease replacement may use a new member generation
                    # while the prior incarnation still has an unresolved
                    # probe. Reuse that state by origin so the old daemon
                    # remains the single in-flight fence for this endpoint.
                    state = unresolved_by_origin.get(member.advertisement.origin)
                reused_unresolved = state is not None and state in retained
                if state is None:
                    if len(retained) + len(existing) >= self._max_workers:
                        omitted += 1
                        continue
                    worker = member.advertisement
                    state = _WorkerState(WorkerEndpoint(worker.origin, worker.worker_id),
                                         advertisement=worker)
                else:
                    existing.pop(key, None)
                    if reused_unresolved:
                        worker = member.advertisement
                        state.endpoint = WorkerEndpoint(worker.origin, worker.worker_id)
                        state.advertisement = worker
                    if key in new_admissions:
                        # A lease renewed after expiry must obtain new evidence;
                        # a still-fresh capability cache predates this admission.
                        state.capabilities = None
                        state.known_models = None
                        state.capability_checked_at = None
                    elif reused_unresolved:
                        # The old probe belongs to the previous lease. Keep
                        # its in-flight fence, but never carry its cached
                        # evidence into the replacement admission.
                        state.capabilities = None
                        state.known_models = None
                        state.capability_checked_at = None
                state.membership_state = member.lifecycle_state
                state.membership_expires_at = result.roster.snapshot.expires_at
                state.membership_evidence = member.evidence
                if not reused_unresolved:
                    retained.append(state)
            self._membership_roster_applied = result.roster is not None
            self._states = retained
            self._membership_omitted = omitted
            self._roster_generation += 1
            self._condition.notify_all()

    def stop_membership(self) -> None:
        with self._condition:
            for state in self._states:
                if state.membership_state is not None:
                    state.membership_state = "draining"
            self._prune_drained()
            self._condition.notify_all()

    def _keeps_draining_state_locked(self, state: _WorkerState) -> bool:
        """Whether a draining ``state`` must stay in the roster for now.

        In-flight work and probes return to it, and so does a lease parked in
        GPU admission (it holds no slot, so ``inflight`` does not show it).
        """
        return bool(
            state.inflight or state.capability_probe_inflight
            or any(lease.state is state for lease in self._leases.values())
        )

    def _prune_drained(self) -> None:
        remaining = [state for state in self._states
                     if (state.membership_state != "draining"
                         or self._keeps_draining_state_locked(state))]
        if len(remaining) != len(self._states):
            self._states = remaining
            self._roster_generation += 1

    def _membership_admissible(self, state: _WorkerState, now: float) -> bool:
        if state.membership_state is None:
            return self._external_source is None or self._external_source._config.local_fallback
        wall_now = self._membership_clock()
        return (state.membership_state == "active" and state.membership_expires_at is not None
                and wall_now < state.membership_expires_at and state.membership_evidence is not None
                and state.membership_evidence.checked_at <= wall_now < state.membership_evidence.expires_at
                and not self._capabilities_stale(state, now) and not state.capability_probe_failed)

    @property
    def capability_ttl_seconds(self) -> float:
        return self._capability_ttl

    @property
    def capability_routing(self) -> str:
        """Configured evidence policy for request admission."""
        return self._capability_routing

    def refresh_membership_capabilities(self, *, renew_within_seconds: float = 0.0) -> None:
        """Probe membership workers that are stale or would expire soon.

        Only the controller renews membership evidence, and admission requires
        it to be unexpired. The controller passes its refresh interval, so a
        member whose evidence would lapse before the next pass (allowing one
        probe timeout of slack, capped at half the TTL) is renewed now instead
        of going dark until then.
        """
        renew = float(renew_within_seconds)
        if not math.isfinite(renew) or renew < 0:
            raise ValueError("renew_within_seconds must be a finite non-negative number")
        # Never renew evidence younger than half its TTL: an interval at or
        # above the TTL cannot be rescued by renewal and must not turn every
        # pass into a full reprobe.
        window = min(renew + self._probe_timeout, self._capability_ttl / 2) if renew else 0.0
        self._refresh_capabilities(_membership=True, _renew_within=window)

    def membership_evidence(self, roster: MembershipRoster) -> tuple[CapabilityEvidence, ...]:
        if type(roster) is not MembershipRoster:
            raise ValueError("exact membership roster required")
        now = self._clock()
        wall_now = self._membership_clock()
        with self._condition:
            evidence = []
            for state in self._states:
                if (state.advertisement is None or state.membership_state in ("draining", "expired")
                        or self._capabilities_stale(state, now) or state.capability_probe_failed
                        or state.compatibility_error or state.cooldown_until > now):
                    continue
                worker = state.advertisement
                checked = state.capability_checked_at
                if checked is None:
                    continue
                expires = min(checked + timedelta(seconds=self._capability_ttl), roster.snapshot.expires_at)
                if checked <= wall_now < expires:
                    evidence.append(CapabilityEvidence(worker.worker_id, worker.origin,
                                                       worker.member_generation, checked, expires))
            return tuple(evidence)

    @property
    def enabled(self) -> bool:
        return len(self._states) > 1 or (
            self._membership_clock is not None and bool(self._configured_remote_origins))

    @property
    def has_remote_workers(self) -> bool:
        return any(not _is_loopback(state.endpoint.origin) for state in self._states)

    @property
    def has_configured_remote_workers(self) -> bool:
        return bool(self._configured_remote_origins)

    @property
    def configured_origins(self) -> tuple[str, ...]:
        """Immutable normalized primary and workers supplied at construction."""
        return self._configured_origins

    @property
    def origins(self) -> tuple[str, ...]:
        return tuple(state.endpoint.origin for state in self._states)

    def _probe_permit(self, origin: str) -> Callable[[], None]:
        """Take the PROBE admission for one capability probe; return its release.

        Called before the probe's bounded timer starts, so time spent waiting
        for a slot can never read as the worker timing out. The gate bounds its
        own wait (``probe_wait_ms``) and skips remote origins. A refusal is a
        deferred check, not a worker fault: it surfaces as
        ``CapabilityProbeDeferred``, which the refresh skips without touching
        the worker's failure count, circuit or capabilities (the next refresh
        asks again because the cached capabilities stay stale).
        """
        acquire = getattr(self.gpu_admission, "acquire_probe", None)
        if acquire is None:
            return lambda: None
        try:
            return acquire("/api/tags", origin=origin)
        except gpu_admission_module.GpuAdmissionRefused as refused:
            raise CapabilityProbeDeferred(
                "capability probe deferred: %s" % refused.refusal.reason.value) from None

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        if isinstance(error, gpu_admission_module.GpuAdmissionRefused):
            return False
        if isinstance(error, ModelCallError):
            return int(getattr(error, "status", 0) or 0) in _FAILOVER_HTTP_CODES
        if isinstance(error, WorkerPoolError):
            return False
        if isinstance(error, urllib.error.HTTPError):
            return int(error.code or 0) in _FAILOVER_HTTP_CODES
        return isinstance(
            error,
            (
                urllib.error.URLError,
                TimeoutError,
                ConnectionError,
                OSError,
                http.client.IncompleteRead,
            ),
        )

    def _capabilities_stale(self, state: _WorkerState, now: float) -> bool:
        return (
            state.capabilities is None
            or now - state.capabilities.observed_at >= self._capability_ttl
        )

    def _capabilities_renewal_due(self, state: _WorkerState, now: float, window: float) -> bool:
        """True once cached capabilities would expire within ``window`` seconds."""
        return self._capabilities_stale(state, now) or (
            window > 0 and now - state.capabilities.observed_at >= self._capability_ttl - window
        )

    def _normalize_capabilities(
        self, payload: object, *, now: float, measured_ms: float,
    ) -> tuple[WorkerCapabilities, float]:
        model_contexts: dict[str, int] = {}
        loaded_models: tuple[str, ...] = ()
        vram_bytes: int | None = None
        ram_bytes: int | None = None
        kv_cache_type: str | None = None
        supports_tools: dict[str, bool] = {}
        model_active_billions: dict[str, float] = {}
        model_metadata: dict[str, dict[str, Any]] = {}

        if isinstance(payload, WorkerCapabilities):
            protocol = payload.protocol
            version = payload.version
            models = payload.models
            advertised = payload.effective_max_inflight
            model_contexts = dict(payload.model_contexts)
            loaded_models = tuple(payload.loaded_models)
            vram_bytes = payload.vram_bytes
            ram_bytes = payload.ram_bytes
            kv_cache_type = payload.kv_cache_type
            supports_tools = dict(payload.supports_tools)
            model_active_billions = dict(payload.model_active_billions)
            model_metadata = {k: dict(v) for k, v in payload.model_metadata.items()}
        elif isinstance(payload, Mapping):
            protocol = str(payload.get("protocol") or _PROTOCOL)
            version = _safe_scalar(payload.get("version"), limit=80)
            raw_models = payload.get("models") or ()
            if isinstance(raw_models, (str, bytes)) or not hasattr(
                raw_models, "__iter__"
            ):
                raise ValueError("worker models capability must be a collection")
            models = tuple(
                sorted({
                    str(name).strip()[:256]
                    for name in raw_models
                    if str(name).strip()
                })
            )[:_MAX_MODELS_PER_WORKER]
            try:
                advertised = int(payload.get("max_inflight") or self._max_inflight)
            except (TypeError, ValueError):
                advertised = self._max_inflight
            try:
                reported_latency = float(payload.get("latency_ms"))
            except (TypeError, ValueError):
                reported_latency = measured_ms
            if math.isfinite(reported_latency) and reported_latency >= 0:
                measured_ms = reported_latency
            raw_contexts = payload.get("model_contexts")
            if isinstance(raw_contexts, Mapping):
                for name, ctx in list(raw_contexts.items())[:_MAX_MODELS_PER_WORKER]:
                    clean_name = str(name or "").strip()[:256]
                    parsed_ctx = _positive_int_value(ctx)
                    if clean_name and parsed_ctx is not None:
                        model_contexts[clean_name] = parsed_ctx
            raw_loaded = payload.get("loaded_models")
            if isinstance(raw_loaded, (list, tuple, set, frozenset)):
                loaded_models = tuple(
                    sorted({
                        str(name).strip()[:256]
                        for name in raw_loaded
                        if str(name).strip()
                    })
                )[:_MAX_MODELS_PER_WORKER]
            vram_bytes = _positive_int_value(payload.get("vram_bytes"))
            ram_bytes = _positive_int_value(payload.get("ram_bytes"))
            raw_kv = str(payload.get("kv_cache_type") or "").strip().lower()
            if raw_kv in _KV_CACHE_TYPES:
                kv_cache_type = raw_kv
            raw_tools = payload.get("supports_tools")
            if isinstance(raw_tools, Mapping):
                for name, supported in list(raw_tools.items())[:_MAX_MODELS_PER_WORKER]:
                    clean_name = str(name or "").strip()[:256]
                    if clean_name and isinstance(supported, bool):
                        supports_tools[clean_name] = supported
            raw_active = payload.get("model_active_billions")
            if isinstance(raw_active, Mapping):
                for name, active_val in list(raw_active.items())[:_MAX_MODELS_PER_WORKER]:
                    clean_name = str(name or "").strip()[:256]
                    parsed_active = _parse_billions(active_val)
                    if clean_name and parsed_active is not None:
                        model_active_billions[clean_name] = parsed_active
            raw_meta = payload.get("model_metadata")
            if isinstance(raw_meta, Mapping):
                for name, meta_val in list(raw_meta.items())[:_MAX_MODELS_PER_WORKER]:
                    clean_name = str(name or "").strip()[:256]
                    if clean_name and isinstance(meta_val, Mapping):
                        model_metadata[clean_name] = dict(meta_val)
        else:
            raise ValueError("capability prober returned an invalid report")
        if protocol != _PROTOCOL:
            raise ValueError("incompatible worker protocol %r" % protocol)
        if advertised < 1:
            raise ValueError("worker advertised invalid capacity")
        if not math.isfinite(measured_ms) or measured_ms < 0:
            measured_ms = 0.0
        return WorkerCapabilities(
            protocol=protocol,
            version=_safe_scalar(version, limit=80),
            models=models,
            observed_at=now,
            effective_max_inflight=min(self._max_inflight, advertised),
            model_contexts=MappingProxyType(model_contexts),
            loaded_models=loaded_models,
            vram_bytes=vram_bytes,
            ram_bytes=ram_bytes,
            kv_cache_type=kv_cache_type,
            supports_tools=MappingProxyType(supports_tools),
            model_active_billions=MappingProxyType(model_active_billions),
            model_metadata=MappingProxyType({
                k: MappingProxyType(v) for k, v in model_metadata.items()
            }),
        ), round(measured_ms, 3)

    def register_worker(
        self,
        origin: str,
        *,
        allow_remote: bool | None = None,
        trusted_origins: tuple[str, ...] | None = None,
        refresh: bool = True,
    ) -> WorkerSnapshot:
        """Dynamically register or refresh a worker endpoint on the live pool.

        Allows connecting a node (e.g., Node1 over private HTTPS) at runtime
        without recreating the pool, automatically probing its capabilities
        and pooling its discovered models and hardware budget when ``refresh``
        is true.
        """
        effective_allow_remote = self._allow_remote if allow_remote is None else bool(allow_remote)
        effective_trusted = self._trusted_origins if trusted_origins is None else tuple(trusted_origins)
        normalized = validate_worker_origin(
            origin,
            allow_remote=effective_allow_remote,
            trusted_origins=effective_trusted,
        )
        with self._condition:
            existing = next(
                (item for item in self._states if item.endpoint.origin == normalized),
                None,
            )
            if existing is None:
                if len(self._states) >= self._max_workers:
                    raise ValueError(
                        "at most %d Ollama workers are supported" % self._max_workers
                    )
                existing = _WorkerState(
                    WorkerEndpoint(normalized, _worker_id(normalized), _metric_label(len(self._states)))
                )
                self._states.append(existing)
                self._roster_generation += 1
                self._local_origins = all(
                    _is_local_origin(item.endpoint.origin) for item in self._states
                )
                if not _is_loopback(normalized):
                    self._configured_remote_origins = self._configured_remote_origins | {normalized}
                    self._allow_remote = self._allow_remote or effective_allow_remote
                if (
                    effective_allow_remote
                    and self._capability_prober is not None
                    and getattr(self._capability_prober, "_sonder_default_allow_remote", None) is False
                ):
                    self._capability_prober = _default_capability_prober(
                        allow_remote=True, timeout=self._probe_timeout,
                    )
                self._condition.notify_all()
            target_id = id(existing)
        if refresh and self._capability_prober is not None:
            self._refresh_capabilities(force=True, _targets=frozenset({target_id}))
        with self._condition:
            return self._snapshot(existing, self._clock())

    def _worker_metric_label(self, state: _WorkerState) -> str:
        # Reserve only when observed: provisional configured endpoints must
        # not consume slots before receiving an admitted member identity.
        if state.metric_label is None:
            if state.advertisement is not None:
                identity = ("member", *self._membership_authority,
                            state.advertisement.worker_id)
            else:
                identity = ("configured-origin", state.endpoint.origin)
            digest = hashlib.sha256(json.dumps(identity, separators=(",", ":"),
                                                ensure_ascii=True).encode("ascii")).hexdigest()
            state.metric_label = self._metric_label_registry.reserve_ollama_worker_label(digest)
        return state.metric_label

    def _record_transport_failure(
        self, state: _WorkerState, error: BaseException,
    ) -> None:
        was_open = state.cooldown_until > self._clock()
        now = self._clock()
        state.consecutive_failures += 1
        state.last_error = self._redactor.redact(_safe_error(error))[:200]
        self._metrics["transport_failures"] += 1
        if (
            state.consecutive_failures > 0
            and state.consecutive_failures < self._failure_threshold
        ):
            logger.warning(
                f"worker {state.endpoint.worker_id} at "
                f"{state.consecutive_failures}/{self._failure_threshold} "
                f"consecutive failures, next failure opens circuit"
            )
        if state.consecutive_failures >= self._failure_threshold:
            exponent = min(3, state.consecutive_failures - self._failure_threshold)
            state.trips += 1
            cooldown_duration = self._cooldown_seconds * (2 ** exponent)
            state.cooldown_until = now + cooldown_duration
            logger.debug(
                f"circuit opened for {state.endpoint.worker_id}: "
                f"failures={state.consecutive_failures}, trips={state.trips}, "
                f"cooldown={cooldown_duration:.1f}s"
            )
            logger.error(
                f"worker {state.endpoint.worker_id} circuit opened after "
                f"{state.consecutive_failures} consecutive transport failures, "
                f"cooldown={cooldown_duration:.1f}s, trips={state.trips}, "
                f"last_error={state.last_error!r}"
            )
            if not was_open:
                logger.warning(
                    f"circuit opened: worker {state.endpoint.worker_id} marked "
                    f"unhealthy after {state.consecutive_failures} consecutive "
                    f"failures, cooldown={cooldown_duration:.1f}s, trips={state.trips}"
                )
            all_unhealthy = all(
                s.cooldown_until > now
                or s.compatibility_error
                or (s.capabilities is None and s.capability_probe_failed)
                for s in self._states
            )
            if all_unhealthy:
                logger.critical(
                    f"all {len(self._states)} worker(s) are unhealthy — "
                    f"no inference capacity remains, "
                    f"worker_ids={[s.endpoint.worker_id for s in self._states]}"
                )
            if not was_open and self._metrics_observer is not None:
                self._metrics_observer.observe_ollama_worker_circuit(
                    worker=self._worker_metric_label(state), state="open"
                )

    def _record_success(
        self, state: _WorkerState, latency_ms: float, *, inference: bool = False,
    ) -> None:
        reconnect = state.consecutive_failures >= self._failure_threshold
        if reconnect:
            logger.debug(f"worker {state.endpoint.worker_id} reconnected after {state.consecutive_failures} failures")
            logger.warning(
                f"worker {state.endpoint.worker_id} recovered after "
                f"{state.consecutive_failures} consecutive failures "
                f"(was circuit-open for {state.trips} trip(s))"
            )
        state.consecutive_failures = 0
        state.last_error = ""
        state.cooldown_until = 0.0
        state.trips = 0
        state.half_open_inflight = False
        state.half_open_owner = None
        if inference:
            state.inference_ewma_ms = (
                latency_ms if state.inference_ewma_ms is None
                else 0.25 * latency_ms + 0.75 * state.inference_ewma_ms
            )
        elif state.latency_ewma_ms is None:
            state.latency_ewma_ms = latency_ms
        else:
            state.latency_ewma_ms = 0.25 * latency_ms + 0.75 * state.latency_ewma_ms
        if reconnect:
            self._metrics["reconnects"] += 1
            if self._metrics_observer is not None:
                self._metrics_observer.observe_ollama_worker_circuit(
                    worker=self._worker_metric_label(state), state="closed"
                )

    def refresh_capabilities(
        self, *, force: bool = False, _membership: bool = False,
    ) -> None:
        self._refresh_capabilities(force=force, _membership=_membership)

    def _refresh_capabilities(
        self, *, force: bool = False, _membership: bool = False,
        _lock_owned: bool = False,
        _targets: frozenset[int] | None = None, _renew_within: float = 0.0,
    ) -> None:
        """Update cached capabilities using the configured bounded probe batch.

        Circuit-open workers are not probed before their retry deadline unless
        ``force`` is explicitly requested by an operator-facing caller.
        Presentation is separate: callers may request a cached status page
        after refresh without constructing whole-roster snapshots here.
        """
        logger.debug(f"refresh_capabilities called, force={force}")
        if self._capability_prober is None:
            logger.debug("no capability prober configured, skipping refresh")
            return
        if not _lock_owned and not self._probe_lock.acquire(blocking=False):
            logger.debug("probe lock contended, skipping refresh")
            return
        try:
            now = self._clock()
            with self._condition:
                candidates = []
                last_selected_index = None
                state_count = len(self._states)
                start = self._probe_cursor % state_count if state_count else 0
                for offset in range(state_count):
                    index = (start + offset) % state_count
                    state = self._states[index]
                    if _targets is not None and id(state) not in _targets:
                        continue
                    if state.membership_state is not None and (
                        state.membership_state in ("draining", "expired")
                        or state.membership_expires_at is None
                        or self._membership_clock() >= state.membership_expires_at
                    ):
                        continue
                    # The controller's pass is the only periodic refresh, so it
                    # also renews the static loopback lane. Skipping it let the
                    # local primary's evidence lapse after one TTL while a remote
                    # member that advertises the model kept every request.
                    # An external source without local_fallback disables that
                    # lane (open_url refuses it), so it is not renewed either.
                    if (_membership and state.membership_state is None
                            and not (_is_loopback(state.endpoint.origin)
                                     and self._membership_admissible(state, now))):
                        continue
                    if not (
                        (force or self._capabilities_renewal_due(state, now, _renew_within))
                        and (force or state.cooldown_until <= now)
                        and not state.half_open_inflight
                        and not state.capability_probe_inflight
                    ):
                        continue
                    candidates.append(state)
                    last_selected_index = index
                    if len(candidates) >= self._probe_batch_size:
                        break
                if last_selected_index is not None and state_count:
                    self._probe_cursor = (last_selected_index + 1) % state_count
            if not candidates:
                logger.debug("no stale/eligible workers to probe")
                return

            def run(state: _WorkerState):
                def clear_inflight():
                    with self._condition:
                        state.capability_probe_inflight = False
                        self._condition.notify_all()

                # Admission first, outside the bounded timer; the slot is held
                # until the probe thread really finishes (even past a timeout).
                try:
                    release = self._probe_permit(state.endpoint.origin)
                except BaseException as refused:
                    clear_inflight()
                    if isinstance(refused, CapabilityProbeDeferred):
                        return None, 0.0, refused
                    raise

                def complete():
                    try:
                        release()
                    finally:
                        clear_inflight()

                started = self._clock()
                payload, error = _bounded_capability_probe(
                    self._capability_prober,
                    state.endpoint.origin,
                    self._probe_timeout,
                    complete,
                )
                elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
                return payload, elapsed_ms, error

            def probe_identity(state):
                advertisement = state.advertisement
                return (
                    state.endpoint.origin,
                    state.endpoint.worker_id,
                    state.membership_state,
                    advertisement.origin if advertisement is not None else None,
                    advertisement.worker_id if advertisement is not None else None,
                    advertisement.member_generation if advertisement is not None else None,
                )

            logger.debug(
                f"probing {len(candidates)} candidate workers: "
                f"{[s.endpoint.worker_id for s in candidates]}"
            )
            logger.info(f"probing capabilities on {len(candidates)} worker(s)")
            with self._condition:
                for state in candidates:
                    state.capability_probe_inflight = True
                selected_identities = {
                    id(state): probe_identity(state) for state in candidates
                }
            workers = min(self._probe_parallelism, len(candidates))
            submitted = []
            try:
                with owned_runtime_pool(max_workers=workers) as executor:
                    futures = []
                    for state in candidates:
                        futures.append(executor.submit(run, state))
                        submitted.append(state)
                    outcomes = [future.result() for future in futures]
            except BaseException:
                # A pool construction or submission failure happens before a
                # probe callback can clear the marker.  Clear only states that
                # were never submitted; submitted work owns its marker until
                # its bounded daemon callback finishes.
                with self._condition:
                    for state in candidates:
                        if state not in submitted:
                            state.capability_probe_inflight = False
                    self._condition.notify_all()
                raise

            with self._condition:
                for state, (payload, measured_ms, error) in zip(candidates, outcomes):
                    state.capability_probe_generation += 1
                    if state.membership_state == "draining" or not any(current is state for current in self._states):
                        continue
                    if probe_identity(state) != selected_identities[id(state)]:
                        self._metrics["capability_probe_failures"] += 1
                        state.capability_probe_failed = True
                        state.last_error = "capability probe result discarded after membership change"
                        logger.warning(
                            "discarding capability probe result for %s after membership identity changed",
                            state.endpoint.worker_id,
                        )
                        continue
                    self._metrics["capability_probes"] += 1
                    if isinstance(error, CapabilityProbeDeferred):
                        logger.info(
                            "capability probe for worker %s deferred by gpu admission; will retry",
                            state.endpoint.worker_id)
                        continue
                    if error is not None:
                        logger.debug(f"capability probe failed for {state.endpoint.worker_id}: {_safe_error(error)}")
                        logger.warning(
                            f"capability probe failed for worker "
                            f"{state.endpoint.worker_id}: {_safe_error(error)} "
                            f"(worker may be overloaded or unreachable)"
                        )
                        logger.error(
                            f"capability probe failed for worker "
                            f"{state.endpoint.worker_id}, probe_error={_safe_error(error)!r}",
                            exc_info=error,
                        )
                        self._metrics["capability_probe_failures"] += 1
                        state.capability_probe_failed = True
                        if self._retryable(error):
                            self._record_transport_failure(state, error)
                        else:
                            state.compatibility_error = self._redactor.redact(_safe_error(error))
                            state.last_error = state.compatibility_error
                        continue
                    try:
                        capabilities, latency_ms = self._normalize_capabilities(
                            payload, now=self._clock(), measured_ms=measured_ms,
                        )
                    except (TypeError, ValueError) as capability_error:
                        self._metrics["capability_probe_failures"] += 1
                        state.capability_probe_failed = True
                        state.compatibility_error = self._redactor.redact(_safe_error(capability_error))
                        state.last_error = state.compatibility_error
                        logger.error(
                            f"capability probe returned unexpected data for worker "
                            f"{state.endpoint.worker_id}, "
                            f"compatibility_error={state.compatibility_error!r}",
                            exc_info=True,
                        )
                        continue
                    logger.debug(
                        f"capability probe succeeded for {state.endpoint.worker_id}: "
                        f"models={len(capabilities.models)}, version={capabilities.version!r}, "
                        f"effective_max_inflight={capabilities.effective_max_inflight}, latency={latency_ms:.1f}ms"
                    )
                    state.capabilities = capabilities
                    if state.membership_state is not None:
                        state.capability_checked_at = self._membership_clock()
                    state.compatibility_error = ""
                    state.capability_probe_failed = False
                    self._record_success(state, latency_ms)
                self._condition.notify_all()
        finally:
            with self._condition:
                self._condition.notify_all()
            if not _lock_owned:
                self._probe_lock.release()

    def _refresh_for_model(self, model: str, *, admission_timeout: float) -> None:
        """Probe one fair wave of static unknown/stale workers, joining existing work.

        Probing a single worker per request failed any request whose model was
        served by another unknown/stale worker: the fair cursor picked one
        worker, it lacked the model, and admission refused the request at once
        although the next worker in rotation advertised it. One wave is at
        most the configured probe parallelism (and batch size), so its probes
        run concurrently, take no longer than one probe timeout, and never
        fan out across a large roster. A request takes part in at most one
        wave: a caller that arrives while another request's wave is in flight
        joins that wave and starts none of its own. A caller that waited
        behind an administrative or controller batch starts its wave only if
        the model is still unserved, and only over workers that batch did not
        probe.

        The same probe lock serializes this with administrative batches, so
        their configured parallelism ceiling remains global to this pool.
        Waiters consume the existing global admission queue. Membership lease
        activation/renewal remains exclusively owned by the controller.
        """
        with self._condition:
            now = self._clock()
            if self._draining or self._capability_prober is None:
                return
            if self._fresh_supporter_locked(model, now):
                return
            candidates = [state for state in self._states
                          if self._stale_static_candidate_locked(state, now)]
            if not candidates:
                miss = self._model_miss_candidates_locked(model, now)
                if not miss:
                    return
                self._last_model_miss_refresh = now
            else:
                # Candidates do not depend on the model, so another request's
                # wave in flight is the wave this caller would have started.
                joined = self._request_wave_inflight
                # A lock holder bumps the completion generation of every worker
                # it probes, after either a successful or failed result, so a
                # caller that waited never probes the same worker again.
                generations = {id(state): state.capability_probe_generation
                               for state in candidates}
                acquired = self._probe_lock.acquire(blocking=False)
                if not acquired:
                    if self._waiters >= self._queue_depth:
                        self._metrics["backpressure_rejections"] += 1
                        raise WorkerPoolBackpressure("Ollama worker queue is full")
                    self._waiters += 1
        if not candidates:
            self._refresh_after_model_miss(model, miss, admission_timeout=admission_timeout)
            return
        if not acquired:
            try:
                acquired = self._probe_lock.acquire(timeout=max(0.0, admission_timeout))
            finally:
                with self._condition:
                    self._waiters -= 1
                    self._condition.notify_all()
            if not acquired:
                with self._condition:
                    self._metrics["backpressure_rejections"] += 1
                raise WorkerPoolBackpressure("timed out waiting for Ollama worker capability")
        started = False
        try:
            with self._condition:
                now = self._clock()
                # The batch this caller waited behind may already have found
                # the model; never start another wave in that case.
                if self._draining or joined or self._fresh_supporter_locked(model, now):
                    return
                # Fair rotation from the shared cursor, chosen only now that
                # the lock is held. A worker whose earlier probe has not
                # returned would be skipped by the refresh, so it must not
                # take a wave slot.
                count = len(self._states)
                start = self._probe_cursor % count if count else 0
                wave = [state for state in self._states[start:] + self._states[:start]
                        if generations.get(id(state)) == state.capability_probe_generation
                        and self._stale_static_candidate_locked(state, now)
                        and not state.capability_probe_inflight
                        ][:min(self._probe_batch_size, self._probe_parallelism)]
                if not wave:
                    return
                self._request_wave_inflight = started = True
            self._refresh_capabilities(
                _targets=frozenset(id(state) for state in wave), _lock_owned=True)
        finally:
            if started:
                with self._condition:
                    self._request_wave_inflight = False
            self._probe_lock.release()

    def _stale_static_candidate_locked(self, state: _WorkerState, now: float) -> bool:
        """True for a routable static worker whose inventory is unknown or expired."""
        return (state.membership_state is None
                and self._membership_admissible(state, now)
                and self._capabilities_stale(state, now)
                and state.cooldown_until <= now and not state.half_open_inflight)

    def _fresh_supporter_locked(self, model: str, now: float) -> bool:
        """True when an admissible, closed-circuit worker freshly advertises ``model``."""
        return any(self._membership_admissible(state, now)
                   and not state.compatibility_error and state.cooldown_until <= now
                   and self._supports_model(state, model) for state in self._states)

    def _model_miss_candidates_locked(self, model: str, now: float) -> list[_WorkerState]:
        """Select at most one probe batch whose cached inventory may predate a pull.

        Only workers that are already admissible, or whose membership lease is
        current and whose capability evidence is still valid, qualify: this
        refresh renews inventory, it never activates or renews membership.
        """
        last = self._last_model_miss_refresh
        if last is not None and now - last < _MODEL_MISS_REFRESH_INTERVAL_SECONDS:
            return []
        wall_now = self._membership_clock() if self._membership_clock is not None else None
        selected = []
        for state in self._states:
            if state.membership_state is None:
                if not self._membership_admissible(state, now):
                    continue
            elif not (state.membership_state == "active"
                      and state.membership_expires_at is not None
                      and wall_now < state.membership_expires_at
                      and state.membership_evidence is not None
                      and state.membership_evidence.checked_at <= wall_now
                      < state.membership_evidence.expires_at):
                continue
            if (state.compatibility_error or state.cooldown_until > now
                    or state.half_open_inflight or state.capability_probe_inflight
                    or state.capabilities is None or self._supports_model(state, model)):
                continue
            selected.append(state)
            if len(selected) >= self._probe_batch_size:
                break
        return selected

    def _refresh_after_model_miss(self, model: str, targets, *, admission_timeout: float) -> None:
        wait = max(0.0, min(admission_timeout, self._probe_timeout))
        if not self._probe_lock.acquire(timeout=wait):
            # Another bounded probe owns the lock; never queue a second one.
            return
        try:
            logger.info(
                "model %r is not advertised by any eligible worker; refreshing "
                "capabilities on %d worker(s) once", model[:128], len(targets),
            )
            with self._condition:
                self._metrics["model_miss_refreshes"] += 1
            self._refresh_capabilities(
                force=True, _targets=frozenset(id(state) for state in targets), _lock_owned=True,
            )
        finally:
            self._probe_lock.release()

    def _supports_model(self, state: _WorkerState, model: str | None) -> bool:
        if not model:
            return True
        if state.capabilities is None:
            return False
        if self._capabilities_stale(state, self._clock()):
            return False
        wanted = _model_key(model)
        return any(_model_key(name) == wanted for name in state.capabilities.models)

    def note_models(self, origin_or_id: str, model_names) -> bool:
        target = str(origin_or_id or "").strip().rstrip("/")
        names = frozenset(_model_key(name) for name in (model_names or ()) if _model_key(name))
        logger.debug(f"note_models: target={target!r}, model_count={len(names)}")
        with self._condition:
            for state in self._states:
                if target in (state.endpoint.origin, state.endpoint.worker_id):
                    state.known_models = names
                    logger.debug(f"note_models: updated {state.endpoint.worker_id} with {len(names)} models")
                    return True
        logger.debug(f"note_models: no matching worker for target={target!r}")
        return False

    def refresh_inventory(self, fetch_tags: Callable[[str], object]) -> dict:
        results = {}
        with self._condition:
            endpoints = [state.endpoint for state in self._states]
        for endpoint in endpoints:
            try:
                rows = model_inventory.inventory_rows(
                    fetch_tags(endpoint.origin), "/api/tags"
                )
                names = [row.get("name") or row.get("model") for row in rows]
                self.note_models(endpoint.origin, names)
                results[endpoint.worker_id] = len([name for name in names if name])
            except Exception as error:
                logger.error(
                    f"inventory refresh failed for worker {endpoint.worker_id}, "
                    f"error={_safe_error(error)!r}",
                    exc_info=True,
                )
                results[endpoint.worker_id] = "error: %s" % _safe_error(error)
        return results

    def catalog_member(self, origin: str) -> bool:
        """Whether ``origin`` is a member ``catalog_union`` would read right now.

        That is the admissibility ``_choose`` applies (membership, no
        compatibility error), ignoring load and cooldown.  An external roster
        without ``local_fallback`` never admits the loopback primary.
        """
        wanted = str(origin or "").rstrip("/")
        with self._condition:
            now = self._clock()
            return any(
                state.endpoint.origin == wanted and self._membership_admissible(state, now)
                and not state.compatibility_error
                for state in self._states
            )

    def catalog_union(
        self, fetch_tags: Callable[[str], object], *, routable_only: bool = False,
    ) -> dict:
        """One ``/api/tags``-shaped catalog holding every member's models.

        A pooled request reaches whichever member the scheduler picks, so a
        catalog read through ``request`` describes one host. Tier validation
        needs the pool's catalog: a model counts as installed when any member
        holds it. The first member (the primary) wins a name both hold. An
        unreachable member is skipped; if none answers, the last error is raised.
        Only members the scheduler could route to count -- the same
        admissibility ``_choose`` applies (not probationary, expired or draining,
        no compatibility error) -- so a model is never "installed" on a worker
        that could not serve it. ``routable_only`` additionally skips members
        whose circuit is open (``cooldown_until`` in the future), so a pin is
        never resolved against a worker ``_choose`` would refuse right now.
        """
        with self._condition:
            now = self._clock()
            origins = [
                state.endpoint.origin for state in self._states
                if self._membership_admissible(state, now) and not state.compatibility_error
                and not (routable_only and state.cooldown_until > now)
            ]
        merged: dict[str, dict] = {}
        answered, last_error = False, None
        for origin in origins:
            try:
                payload = fetch_tags(origin)
            except Exception as error:
                logger.warning(f"catalog read skipped a worker: {_safe_error(error)!r}")
                last_error = error
                continue
            payload = payload or {}
            rows = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(payload, dict) or (
                rows is not None and not isinstance(rows, (list, tuple))
            ):
                last_error = ValueError("malformed catalog payload from a worker")
                logger.warning(f"catalog read skipped a worker: {last_error!r}")
                continue
            answered = True
            for row in rows or ():
                name = isinstance(row, dict) and (row.get("name") or row.get("model"))
                # A worker controls this payload: only a non-empty string names a model
                # (anything else, e.g. a list, is skipped rather than hashed).
                if isinstance(name, str) and name and name not in merged:
                    merged[name] = row
        if not answered and last_error is not None:
            raise last_error
        return {"models": list(merged.values())}

    def _capacity(self, state: _WorkerState) -> int:
        if state.capabilities is None:
            return self._max_inflight
        return state.capabilities.effective_max_inflight

    def _choose(
        self, *, model: str | None, excluded: set[str], now: float,
        evidence_allowed: set[str] | None = None,
        required_num_ctx: int | None = None,
    ) -> _WorkerState | None:
        candidates = [
            state for state in self._states
            if state.endpoint.worker_id not in excluded
            and self._membership_admissible(state, now)
            and not state.compatibility_error
            and self._supports_model(state, model)
            and (evidence_allowed is None or state.endpoint.worker_id in evidence_allowed)
            and state.cooldown_until <= now
            and not state.half_open_inflight
            and state.unpark_waiters == 0
            and state.inflight < self._capacity(state)
        ]
        if not candidates:
            logger.debug(f"_choose: no eligible workers for model={model!r}, excluded={excluded}")
            return None
        half_open = [
            state for state in candidates
            if state.consecutive_failures >= self._failure_threshold
        ]
        if half_open:
            candidates = half_open
        start = self._cursor % len(candidates)
        self._cursor += 1
        rotated = candidates[start:] + candidates[:start]
        # Model-less reads rank by control-plane RTT. Inference ranks by
        # inference time only once every candidate has one; until then it
        # uses control-plane RTT for all, so the two scales never mix.
        by_inference = model is not None and all(
            state.inference_ewma_ms is not None for state in candidates
        )
        target_model_key = _model_key(model) if model else ""
        wanted_ctx = (
            int(required_num_ctx)
            if isinstance(required_num_ctx, int)
            and not isinstance(required_num_ctx, bool)
            and required_num_ctx > 0
            else None
        )

        def score(state: _WorkerState):
            latency = state.inference_ewma_ms if by_inference else state.latency_ewma_ms
            if latency is None:
                latency = 1000.0
            lacks_model = (
                1 if model and state.known_models is not None
                and target_model_key not in state.known_models else 0
            )
            caps = state.capabilities
            if model and caps is not None:
                loaded_keys = {_model_key(item) for item in caps.loaded_models}
                not_resident = 0 if target_model_key in loaded_keys else 1
                worker_ctx = caps.context_for(model)
                ctx_headroom_miss = (
                    1 if wanted_ctx is not None and worker_ctx is not None and worker_ctx < wanted_ctx else 0
                )
            else:
                not_resident = 0
                ctx_headroom_miss = 0
            return (
                lacks_model,
                not_resident,
                ctx_headroom_miss,
                (state.inflight + 1) * latency,
                state.inflight,
            )

        chosen = min(rotated, key=score)
        logger.debug(
            f"_choose: selected {chosen.endpoint.worker_id} for model={model!r}, "
            f"inflight={chosen.inflight}/{self._capacity(chosen)}, "
            f"latency_ewma={chosen.latency_ewma_ms}, candidates={len(candidates)}"
        )
        return chosen

    def _acquire(
        self,
        *,
        model: str | None,
        excluded: set[str],
        admission_timeout: float,
        evidence_allowed: set[str] | None = None,
        evidence_reason: str = "",
        required_num_ctx: int | None = None,
        _lease_box: list | None = None,
        _lease_caller: str = "",
    ) -> _WorkerState:
        """Count one slot on a chosen worker and return that worker.

        With ``_lease_box``, the slot's lease is opened and appended to it in
        the same critical section as the count, so no exit path between the
        two can leave a slot counted without a lease to close it.
        """
        logger.debug(f"_acquire: model={model!r}, excluded={excluded}, timeout={admission_timeout:.3f}s")
        deadline = time.monotonic() + admission_timeout
        queued = False
        with self._condition:
            while True:
                if self._draining:
                    if queued:
                        self._waiters -= 1
                    self._metrics["drain_rejections"] += 1
                    raise WorkerPoolDraining("Ollama worker pool is draining")
                now = self._clock()
                state = self._choose(model=model, excluded=excluded, now=now,
                                     evidence_allowed=evidence_allowed,
                                     required_num_ctx=required_num_ctx)
                if state is not None:
                    if self._metrics_observer is not None:
                        self._worker_metric_label(state)
                    half_open = state.consecutive_failures >= self._failure_threshold
                    lease = None
                    if _lease_box is not None:
                        lease = self._new_lease(state, model=model, caller=_lease_caller)
                    state.inflight += 1
                    if lease is not None:
                        self._leases[lease.id] = lease
                        _lease_box.append(lease)
                    if half_open:
                        state.half_open_inflight = True
                        state.half_open_owner = lease.id if lease is not None else None
                    if queued:
                        self._waiters -= 1
                    return state

                remaining_states = [
                    state for state in self._states
                    if state.endpoint.worker_id not in excluded
                    and self._membership_admissible(state, now)
                    and not state.compatibility_error
                    and self._supports_model(state, model)
                    and (evidence_allowed is None
                         or state.endpoint.worker_id in evidence_allowed)
                ]
                if not remaining_states:
                    if queued:
                        self._waiters -= 1
                    available = [state for state in self._states if self._membership_admissible(state, now)]
                    if evidence_allowed is not None and not evidence_allowed and available:
                        raise WorkerCapabilityUnavailable(
                            "no Ollama worker passed required capability evidence%s"
                            % ((": " + evidence_reason) if evidence_reason else "")
                        )
                    if model and available:
                        raise WorkerCapabilityUnavailable(
                            "no Ollama worker advertises model %r" % model
                        )
                    raise WorkerPoolUnavailable("no Ollama worker is available")

                saturated = any(
                    state.cooldown_until <= now
                    and (state.inflight >= self._capacity(state) or state.unpark_waiters)
                    for state in remaining_states
                )
                if not saturated:
                    if queued:
                        self._waiters -= 1
                    retry_after = min(
                        max(0.0, state.cooldown_until - now)
                        for state in remaining_states
                    )
                    raise WorkerPoolUnavailable(
                        "all Ollama workers are unavailable; retry after %.3fs"
                        % retry_after, retry_after_seconds=retry_after,
                    )
                if not queued:
                    if self._waiters >= self._queue_depth:
                        logger.debug(f"_acquire: queue full ({self._waiters}/{self._queue_depth}), rejecting")
                        logger.warning(
                            f"worker pool queue full, rejecting request: "
                            f"waiters={self._waiters}/{self._queue_depth}"
                        )
                        self._metrics["backpressure_rejections"] += 1
                        raise WorkerPoolBackpressure("Ollama worker queue is full")
                    self._waiters += 1
                    queued = True
                    if self._queue_depth > 0 and self._waiters >= self._queue_depth * 0.8:
                        logger.warning(
                            f"worker pool queue depth approaching limit: "
                            f"waiters={self._waiters}/{self._queue_depth}"
                        )
                    logger.debug(f"_acquire: queued for capacity, waiters={self._waiters}/{self._queue_depth}")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reclaimed = self._leases_reclaimed
                    self._reconcile_leases_locked(now)
                    if self._leases_reclaimed != reclaimed:
                        continue  # a dead holder's slot was just returned
                    self._waiters -= 1
                    self._metrics["backpressure_rejections"] += 1
                    holder = self._lease_holder_note_locked(remaining_states, now)
                    logger.warning(
                        f"admission timeout after {admission_timeout:.3f}s "
                        f"waiting for worker capacity, model={model!r}, "
                        f"waiters={self._waiters}/{self._queue_depth}{holder}"
                    )
                    raise WorkerPoolBackpressure(
                        "timed out waiting for Ollama worker capacity"
                    )
                self._condition.wait(timeout=remaining)

    def _finish(
        self,
        state: _WorkerState,
        error: BaseException | None = None,
        elapsed: float | None = None,
        *,
        latency_ms: float | None = None,
        count_failure: bool = True,
        _lease: _PoolLease | None = None,
    ) -> None:
        if latency_ms is None:
            latency_ms = max(0.0, float(elapsed or 0.0) * 1000.0)
        with self._condition:
            # A lease parked in GPU admission already returned its slot.
            if _lease is None or _lease.counted:
                state.inflight = max(0, state.inflight - 1)
            if _lease is not None:
                _lease.counted = False
                _lease.closed = True
                self._leases.pop(_lease.id, None)
            if _lease is None or state.half_open_owner == _lease.id:
                # Only the request that holds the half-open probe ends it; a
                # success below also closes it (the worker answered).
                state.half_open_inflight = False
                state.half_open_owner = None
            if error is None:
                if _lease is not None and _lease.model:
                    self._record_success(state, max(
                        0.0, latency_ms - _lease.admission_seconds * 1000.0,
                    ), inference=True)
                else:
                    self._record_success(state, latency_ms)
                result = "ok"
            elif count_failure and self._retryable(error):
                self._record_transport_failure(state, error)
                result = "error"
            else:
                state.last_error = self._redactor.redact(_safe_error(error))[:200]
                result = "error"
            if self._metrics_observer is not None:
                self._metrics_observer.observe_ollama_worker_request(
                    worker=self._worker_metric_label(state),
                    result=result,
                    elapsed_seconds=max(0.0, latency_ms / 1000.0),
                )
            self._prune_drained()
            self._condition.notify_all()

    # -- slot leases --------------------------------------------------------------
    def _new_lease(self, state: _WorkerState, *, model: str | None, caller: str) -> _PoolLease:
        """An unregistered lease on ``state`` owned by the calling thread."""
        return _PoolLease(
            id=next(self._lease_ids), state=state,
            worker_id=state.endpoint.worker_id,
            model=str(model or "")[:128], caller=str(caller or "")[:32],
            thread=threading.current_thread(), started=self._clock(),
        )

    def _close_lease(
        self,
        lease: _PoolLease,
        error: BaseException | None = None,
        *,
        latency_ms: float | None = None,
        count_failure: bool = True,
    ) -> bool:
        """Finish ``lease`` once; the first close wins and later ones are no-ops."""
        with self._condition:
            if lease.closed:
                return False
            self._finish(
                lease.state, error, latency_ms=latency_ms,
                count_failure=count_failure, _lease=lease,
            )
            return True

    def _abandon_lease(self, lease: _PoolLease, error: BaseException | None) -> None:
        """Close a lease its request left open, without blaming the worker."""
        with self._condition:
            phase = lease.phase
            held = max(0.0, self._clock() - lease.started)
            closed = self._close_lease(
                lease, error if error is not None else WorkerPoolError("pool lease abandoned"),
                count_failure=False,
            )
        if closed:
            logger.warning(
                "Ollama pool lease #%d released on abnormal exit: worker=%s caller=%s "
                "model=%s phase=%s held=%.3fs exception=%s",
                lease.id, lease.worker_id, lease.caller, lease.model, phase, held,
                type(error).__name__ if error is not None else "none",
            )

    def _reconcile_leases_locked(self, now: float) -> None:
        """Warn once about long-held leases; reclaim leases whose thread died.

        This "watchdog" is passive: there is no timer or thread. It runs only
        when ``summary()``, ``status()`` or an ``_acquire`` admission timeout
        calls it, so an idle pool reclaims nothing until one of those runs.
        Caller holds ``_condition``. A reclaim restores the invariant that
        ``state.inflight`` equals the open, counted leases on that state.
        """
        for lease in tuple(self._leases.values()):
            age = max(0.0, now - lease.started)
            try:
                alive = bool(lease.thread.is_alive())
            except RuntimeError:
                # Python 3.14's ``_DummyThread.is_alive()`` raises once the
                # foreign thread is gone; that thread can no longer close it.
                alive = False
            thread_name = _safe_scalar(getattr(lease.thread, "name", ""), limit=80)
            if not alive:
                logger.error(
                    "Ollama pool lease #%d reclaimed: thread %s exited without "
                    "releasing it; worker=%s caller=%s model=%s phase=%s age=%.1fs",
                    lease.id, thread_name, lease.worker_id, lease.caller,
                    lease.model, lease.phase, age,
                )
                state = lease.state
                if lease.counted:
                    state.inflight = max(0, state.inflight - 1)
                lease.counted = False
                lease.closed = True
                del self._leases[lease.id]
                if state.half_open_owner == lease.id or not any(
                    other.state is state for other in self._leases.values()
                ):
                    # The dead lease owned the half-open probe, or no live
                    # request on this worker can own it.
                    state.half_open_inflight = False
                    state.half_open_owner = None
                self._leases_reclaimed += 1
                self._condition.notify_all()
                continue
            if not lease.warned and age >= self._lease_warn_after:
                lease.warned = True
                logger.warning(
                    "Ollama pool lease #%d held for %.1fs: worker=%s caller=%s model=%s "
                    "phase=%s thread=%s alive=%s",
                    lease.id, age, lease.worker_id, lease.caller, lease.model,
                    lease.phase, thread_name, alive,
                )

    def _lease_holder_note_locked(self, states, now: float) -> str:
        """``; held by lease #N ...`` for the oldest lease on saturated ``states``."""
        saturated = {
            id(state) for state in states
            if state.cooldown_until <= now and state.inflight >= self._capacity(state)
        }
        holders = [lease for lease in self._leases.values()
                   if lease.counted and id(lease.state) in saturated]
        if not holders:
            return ""
        oldest = min(holders, key=lambda lease: lease.started)
        return "; held by lease #%d caller=%s model=%s phase=%s age=%.1fs" % (
            oldest.id, oldest.caller, oldest.model, oldest.phase,
            max(0.0, now - oldest.started),
        )

    def _lease_summary_locked(self, now: float) -> dict:
        oldest = min(self._leases.values(), key=lambda lease: lease.started, default=None)
        return {
            "held": len(self._leases),
            "oldest_age_seconds": (
                None if oldest is None else round(max(0.0, now - oldest.started), 3)
            ),
            "oldest_phase": None if oldest is None else oldest.phase,
            "reclaimed": self._leases_reclaimed,
        }

    def _park_lease(self, lease: _PoolLease) -> None:
        """Return ``lease``'s slot while its request waits in GPU admission.

        GPU admission calls this (through ``admitted``) on the request thread,
        holding none of its own locks, just before the request first queues.
        The lease stays open and attributable; ``_unpark_lease`` counts it again.
        """
        with self._condition:
            if lease.closed or not lease.counted:
                return
            lease.state.inflight = max(0, lease.state.inflight - 1)
            lease.counted = False
            self._condition.notify_all()
        logger.debug(
            "pool lease #%d parked during GPU admission: worker=%s model=%s",
            lease.id, lease.worker_id, lease.model,
        )

    def _unpark_lease(self, lease: _PoolLease, deadline: float | None) -> None:
        """Count a parked lease again once its worker has a free slot.

        Runs inside GPU admission's context while the request holds a GPU
        slot, so the wait is capped at the local admission timeout (or
        ``deadline``, absolute ``time.monotonic()``, if sooner): a GPU slot
        must not sit unused for the rest of another request's run, and the
        host-memory decision behind it goes stale. Any raise here releases the
        GPU ticket. Fresh requests do not take the worker while this waits.

        Raises ``WorkerPoolDraining`` when the pool drains,
        ``WorkerPoolUnavailable`` if the worker entered cooldown or another
        request holds its half-open probe (nothing was sent; the caller may
        retry), and ``WorkerPoolBackpressure`` at the cap (the same refusal
        as a pool queue timeout; the caller may retry).
        """
        cap = time.monotonic() + self._local_admission_timeout
        if deadline is not None:
            cap = min(cap, deadline)
        state = lease.state
        with self._condition:
            if lease.closed or lease.counted:
                return
            state.unpark_waiters += 1
            try:
                while True:
                    if self._draining:
                        self._metrics["drain_rejections"] += 1
                        raise WorkerPoolDraining("Ollama worker pool is draining")
                    now = self._clock()
                    if state.cooldown_until > now or (
                        state.half_open_inflight and state.half_open_owner != lease.id
                    ):
                        retry_after = max(0.0, state.cooldown_until - now)
                        raise WorkerPoolUnavailable(
                            "Ollama worker %s became unavailable while the request waited "
                            "in GPU admission; retry after %.3fs" % (lease.worker_id, retry_after),
                            retry_after_seconds=retry_after,
                        )
                    remaining = cap - time.monotonic()
                    # A request whose own deadline has passed would only time
                    # out in the transport and be charged to the worker.
                    expired = deadline is not None and deadline <= time.monotonic()
                    if state.inflight < self._capacity(state) and not expired:
                        state.inflight += 1
                        lease.counted = True
                        if state.consecutive_failures >= self._failure_threshold:
                            state.half_open_inflight = True
                            state.half_open_owner = lease.id
                        return
                    if remaining <= 0 or expired:
                        self._metrics["backpressure_rejections"] += 1
                        logger.warning(
                            "pool lease #%d timed out resuming worker %s after GPU "
                            "admission, model=%s%s",
                            lease.id, lease.worker_id, lease.model,
                            self._lease_holder_note_locked((state,), self._clock()),
                        )
                        raise WorkerPoolBackpressure(
                            "timed out waiting for Ollama worker capacity after GPU "
                            "admission; GPU slot released, retry"
                        )
                    self._condition.wait(timeout=remaining)
            finally:
                state.unpark_waiters -= 1
                # Fresh requests queued behind this one may now be eligible.
                self._condition.notify_all()

    def request_primary(self, sender, *, model=None, payload=None, origin=None):
        """Gate the single-host/local-only path without enabling remote failover.

        ``origin`` is the caller's primary base URL; it defaults to the pool's
        configured primary so the dispatch target never changes under the gate.
        """
        from sonder_runtime.application.routing.request_capabilities import (
            check_request_evidence,
            request_requirements,
        )

        origin = origin or self._configured_origins[0]
        if self._capability_routing == "off" or self._recent_evidence is None:
            return sender(origin)
        required = request_requirements(payload)
        if not required:
            return sender(origin)

        def observe():
            identity = None
            try:
                should_observe = self._capability_routing == "strict"
                if self._capability_routing == "advisory":
                    should_observe = self._recent_evidence.has_fresh_failure(
                        "ollama", model, required,
                    )
                if should_observe:
                    identity = self._observe_identity_cached(origin, model, payload)
                allowed, reason = check_request_evidence(
                    self._recent_evidence, model, required, backend="ollama", identity=identity,
                    mode=self._capability_routing,
                )
            except (AttributeError, TypeError, ValueError, OSError):
                if self._capability_routing == "advisory":
                    allowed, reason = True, "unverified: backend_identity_missing"
                else:
                    allowed, reason = False, "backend_identity_missing"
                identity = None
            if not allowed:
                if self._capability_routing == "advisory":
                    logger.warning("fallback_used: primary capability evidence refused: %s", reason)
                else:
                    raise WorkerCapabilityUnavailable("primary capability route refused: " + reason)
            logger.info("capability routing primary mode=%s: %s", self._capability_routing, reason)
            return identity

        before = observe()
        result = sender(origin)
        if self._capability_routing == "strict":
            after = observe()
            if after != before:
                raise WorkerCapabilityUnavailable("backend identity changed during model dispatch")
        return result

    def request(
        self,
        sender: Callable[[str], object],
        *,
        model: str | None = None,
        admission_timeout_seconds: float | None = None,
        caller: str | None = None,
        idempotent: bool = False,
        payload=None,
        required_capabilities=None,
        tools=False,
        structured_output=False,
        has_image=False,
        approx_tokens=0,
        long_context=False,
        required_num_ctx: int | None = None,
    ):
        """Admit and send one logical request with pre-response failover only."""
        model = str(model or "").strip() or None
        effective_num_ctx = required_num_ctx
        if effective_num_ctx is None and isinstance(payload, Mapping):
            opts = payload.get("options")
            if isinstance(opts, Mapping):
                effective_num_ctx = _positive_int_value(opts.get("num_ctx"))
        logger.debug(f"pool.request: model={model!r}, idempotent={idempotent}")
        with self._condition:
            self._metrics["logical_requests"] += 1
        scope = _LOCAL_ADMISSION_SCOPE.get()
        scoped_caller, scoped_timeout = scope or ("interactive", None)
        caller = str(caller or scoped_caller).strip().casefold()
        if caller not in _LOCAL_CALLERS and caller != "interactive":
            raise ValueError("caller must be interactive, agent, autopilot, fleet, or local")
        if admission_timeout_seconds is None:
            if caller in _LOCAL_CALLERS and self._local_origins:
                requested = scoped_timeout
                admission_timeout = self._local_admission_timeout if requested is None else min(
                    requested, self._local_admission_timeout,
                )
            else:
                admission_timeout = self._admission_timeout
        else:
            admission_timeout = float(admission_timeout_seconds)
            if not math.isfinite(admission_timeout) or admission_timeout < 0:
                raise ValueError("admission timeout must be a finite value >= 0")
            admission_timeout = min(admission_timeout, _MAX_ADMISSION_TIMEOUT_SECONDS)
        admission_deadline = time.monotonic() + admission_timeout
        if model:
            self._refresh_for_model(model, admission_timeout=admission_timeout)
        evidence_allowed = None
        evidence_reason = ""
        required = frozenset(required_capabilities or ())
        check_evidence = None
        if self._recent_evidence is not None and self._capability_routing != "off":
            requirements_error = False
            try:
                from sonder_runtime.application.routing.request_capabilities import (
                    check_request_evidence,
            request_requirements,
                )
                check_evidence = check_request_evidence
                required |= request_requirements(
                    payload, tools=tools, structured_output=structured_output,
                    has_image=has_image, approx_tokens=approx_tokens,
                    long_context=long_context,
                )
            except (ImportError, TypeError, ValueError):
                requirements_error = bool(
                    required_capabilities or tools or structured_output or has_image
                    or long_context or approx_tokens or payload is not None
                )
                required = frozenset(required_capabilities or ())
            if requirements_error:
                raise WorkerCapabilityUnavailable("request capability requirements are invalid")
            if required or requirements_error:
                # Evidence reads and host identity resolution stay outside the
                # pool condition lock; selection receives an immutable allow set.
                with self._condition:
                    states = tuple(state for state in self._states
                                   if not state.compatibility_error
                                   and self._supports_model(state, model)
                                   and state.cooldown_until <= self._clock()
                                   and self._membership_admissible(state, self._clock()))
                evidence_allowed = set()
                refusals = []
                evidence_deadline = time.monotonic() + min(admission_timeout, 5.0)
                for index, candidate in enumerate(states):
                    if time.monotonic() >= evidence_deadline:
                        remaining = states[index:]
                        if self._capability_routing == "advisory":
                            evidence_allowed.update(
                                item.endpoint.worker_id for item in remaining
                            )
                        refusals.append(
                            "unverified: capability identity discovery budget exhausted"
                        )
                        break
                    if not model:
                        if self._capability_routing == "advisory":
                            evidence_allowed.add(candidate.endpoint.worker_id)
                        refusals.append(
                            f"{candidate.endpoint.worker_id}: unverified: model identity unavailable"
                        )
                        continue
                    try:
                        identity = None
                        should_observe = self._capability_routing == "strict"
                        if self._capability_routing == "advisory":
                            should_observe = self._recent_evidence.has_fresh_failure(
                                "ollama", model, required,
                            )
                        if should_observe:
                            identity = self._observe_identity_cached(
                                candidate.endpoint.origin, model, payload,
                            )
                        allowed, why = check_request_evidence(
                            self._recent_evidence, model, required,
                            backend="ollama", identity=identity,
                            mode=self._capability_routing,
                        )
                    except (AttributeError, ImportError, TypeError, ValueError, OSError, RuntimeError):
                        if self._capability_routing == "advisory":
                            allowed, why = True, "unverified: recent capability evidence unavailable"
                        else:
                            allowed, why = False, "recent capability evidence unavailable"
                    if allowed:
                        evidence_allowed.add(candidate.endpoint.worker_id)
                        logger.info("worker %s capability route: %s", candidate.endpoint.worker_id, why)
                    else:
                        refusals.append(f"{candidate.endpoint.worker_id}: {why}")
                evidence_reason = "; ".join(refusals)
                if (self._capability_routing == "advisory"
                        and states and not evidence_allowed):
                    evidence_allowed = None
                    evidence_reason = "fallback_used: all eligible workers have measured capability failures; " + evidence_reason
                    logger.warning("%s", evidence_reason)
                if required:
                    logger.info(
                        "capability routing mode=%s model=%s: %s",
                        self._capability_routing, model or "unknown",
                        evidence_reason or "eligible: evidence passed or unverified",
                    )
        attempted: set[str] = set()
        last_error = None
        while True:
            if len(attempted) >= self._max_workers:
                # Reconciliation cannot grow one logical request's retry set.
                raise last_error or WorkerPoolUnavailable("Ollama worker attempt limit reached")
            # ``_acquire`` opens the lease into ``leases`` under its lock, and
            # this ``try`` already covers the call, so every exit closes it.
            leases: list[_PoolLease] = []
            abandoned_by = None
            try:
                try:
                    state = self._acquire(
                        model=model,
                        excluded=attempted,
                        admission_timeout=max(
                            0.0, admission_deadline - time.monotonic(),
                        ),
                        evidence_allowed=evidence_allowed,
                        evidence_reason=evidence_reason,
                        required_num_ctx=effective_num_ctx,
                        _lease_box=leases,
                        _lease_caller=caller,
                    )
                except (WorkerPoolUnavailable, WorkerCapabilityUnavailable):
                    if last_error is not None:
                        logger.error(
                            f"all attempted workers failed for model={model!r}, "
                            f"attempted={attempted}, last_error={_safe_error(last_error)!r}"
                        )
                        raise last_error
                    raise
                lease = leases[0]
                with self._condition:
                    if attempted:
                        self._metrics["failovers"] += 1
                        logger.warning(
                            f"failing over to worker {state.endpoint.worker_id} "
                            f"(attempt #{len(attempted) + 1}, model={model!r}), "
                            f"previous worker(s) failed: {attempted}"
                        )
                    self._metrics["dispatches"] += 1
                attempted.add(state.endpoint.worker_id)
                logger.debug(f"pool.request: dispatching to {state.endpoint.worker_id}, model={model!r}")
                identity = None
                if check_evidence is not None and required:
                    try:
                        should_observe = self._capability_routing == "strict"
                        if self._capability_routing == "advisory":
                            should_observe = self._recent_evidence.has_fresh_failure(
                                "ollama", model, required,
                            )
                        if should_observe:
                            identity = self._observe_identity_cached(
                                state.endpoint.origin, model, payload,
                            )
                        still_allowed, why = check_evidence(
                            self._recent_evidence, model, required,
                            backend="ollama", identity=identity,
                            mode=self._capability_routing,
                        )
                    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
                        if self._capability_routing == "advisory":
                            still_allowed, why = True, "unverified: recent capability evidence unavailable"
                        else:
                            still_allowed, why = False, "recent capability evidence unavailable"
                    if not still_allowed:
                        if self._capability_routing == "advisory":
                            alternatives = (evidence_allowed or set()) - attempted
                            if alternatives:
                                evidence_allowed.discard(state.endpoint.worker_id)
                                self._close_lease(
                                    lease,
                                    error=WorkerCapabilityUnavailable(
                                        f"worker capability evidence changed: {why}"
                                    ),
                                    count_failure=False,
                                )
                                evidence_reason = why
                                continue
                            logger.warning(
                                "fallback_used: capability evidence refused worker %s: %s",
                                state.endpoint.worker_id, why,
                            )
                            evidence_allowed = None
                        else:
                            evidence_allowed.discard(state.endpoint.worker_id)
                    if not still_allowed and self._capability_routing == "strict":
                        self._close_lease(
                            lease,
                            error=WorkerCapabilityUnavailable(
                                f"worker capability evidence changed: {why}"
                            ),
                            count_failure=False,
                        )
                        evidence_reason = why
                        continue
                started = self._clock()
                dispatch_identity = identity
                try:
                    # ``admitted()`` reads this to attribute (and in GPU
                    # admission, park) the slot this sender is using.
                    token = _ACTIVE_LEASE.set((self, lease))
                    try:
                        result = sender(state.endpoint.origin)
                    finally:
                        _ACTIVE_LEASE.reset(token)
                except Exception as error:
                    latency_ms = max(0.0, (self._clock() - started) * 1000.0)
                    retryable = self._retryable(error)
                    logger.debug(
                        f"pool.request: error from {state.endpoint.worker_id} after {latency_ms:.1f}ms, "
                        f"retryable={retryable}: {_safe_error(error)}"
                    )
                    self._close_lease(
                        lease, error=error, latency_ms=latency_ms,
                        count_failure=retryable,
                    )
                    if not idempotent or not retryable:
                        raise
                    logger.error(
                        f"worker {state.endpoint.worker_id} inference request failed, "
                        f"failing over to next worker, model={model!r}, "
                        f"elapsed_ms={latency_ms:.1f}, error={_safe_error(error)!r}",
                        exc_info=True,
                    )
                    last_error = error
                    continue
                latency_ms = max(0.0, (self._clock() - started) * 1000.0)
                logger.debug(f"pool.request: success from {state.endpoint.worker_id} in {latency_ms:.1f}ms")
                if check_evidence is not None and required:
                    try:
                        if self._capability_routing == "strict":
                            identity = self._observe_identity_cached(
                                state.endpoint.origin, model, payload,
                            )
                            still_allowed, why = check_evidence(
                                self._recent_evidence, model, required,
                                backend="ollama", identity=identity,
                                mode=self._capability_routing,
                            )
                        else:
                            # Advisory mode intentionally has no post-dispatch identity
                            # observation.  A failed preflight remains a conservative
                            # routing hint; dispatch itself is already complete.
                            still_allowed, why = True, "unverified" if identity is None else ""
                        if identity != dispatch_identity and (
                            self._capability_routing == "strict"
                            or identity is not None and dispatch_identity is not None
                        ):
                            still_allowed, why = False, "backend_identity_changed"
                    except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
                        still_allowed = self._capability_routing == "advisory"
                        why = "unverified: recent capability evidence unavailable"
                    if still_allowed and why:
                        logger.info("worker %s capability route after response: %s", state.endpoint.worker_id, why)
                    if not still_allowed:
                        self._close_lease(
                            lease,
                            error=WorkerCapabilityUnavailable(
                                f"worker capability evidence changed after response: {why}"
                            ),
                            count_failure=False,
                        )
                        raise WorkerCapabilityUnavailable(
                            f"worker capability evidence changed after response: {why}"
                        )
                self._close_lease(lease, error=None, latency_ms=latency_ms)
                return result
            except BaseException as error:
                abandoned_by = error
                raise
            finally:
                # Any exit that did not close the lease (a BaseException, or
                # an evidence check raising outside its caught tuple) must
                # still return the slot, or the worker is lost until restart.
                for opened in leases:
                    if not opened.closed:
                        self._abandon_lease(opened, abandoned_by)

    def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        """Stop admission and wait a bounded interval for in-flight calls."""
        logger.debug(f"pool.drain: starting with timeout={timeout_seconds}s")
        logger.info(f"worker pool drain started, timeout={timeout_seconds}s")
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            self._draining = True
            self._condition.notify_all()
            while any(state.inflight for state in self._states):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    inflight_count = sum(s.inflight for s in self._states)
                    logger.warning(
                        f"worker pool drain timed out after {timeout_seconds}s "
                        f"with {inflight_count} in-flight request(s) remaining"
                    )
                    return False
                self._condition.wait(timeout=remaining)
            logger.info("worker pool drained successfully")
            return True

    def model_capacity(self, model: str | None) -> int | None:
        """Admission slots the pool can grant for ``model`` right now.

        Mirrors ``_choose``: per admissible, non-cooling worker whose fresh
        capability evidence advertises the model, the free slots are capacity
        minus inflight, except that a half-open worker (failure threshold
        reached) takes a single recovery probe and none while one is in
        flight. Returns ``None`` when no worker advertises the model, because
        "unknown" must not be read as "zero"; ``0`` means every advertising
        worker is busy.
        """
        now = self._clock()
        advertised, total = False, 0
        with self._condition:
            for state in self._states:
                if not (
                    self._membership_admissible(state, now)
                    and not state.compatibility_error
                    and state.cooldown_until <= now
                    and self._supports_model(state, model)
                ):
                    continue
                advertised = True
                if state.half_open_inflight:
                    continue
                free = max(0, self._capacity(state) - state.inflight)
                if state.consecutive_failures >= self._failure_threshold:
                    free = min(1, free)
                total += free
        return total if advertised else None

    def snapshots(self) -> tuple[WorkerSnapshot, ...]:
        now = self._clock()
        with self._condition:
            return tuple(self._snapshot(state, now) for state in self._states)

    def _snapshot(self, state: _WorkerState, now: float) -> WorkerSnapshot:
        """Copy one selected worker while the pool condition is held."""
        stale = self._capabilities_stale(state, now)
        healthy = (
            self._membership_admissible(state, now)
            and not state.compatibility_error
            and not (
                state.capabilities is None
                and state.capability_probe_failed
            )
            and state.cooldown_until <= now
        )
        capacity = self._capacity(state)
        if self._draining:
            label = "draining" if state.inflight else "drained"
        elif state.membership_state == "draining":
            label = "draining"
        elif state.membership_state is not None and state.membership_expires_at is not None and (
            self._membership_clock() >= state.membership_expires_at
        ):
            label = "expired"
        elif state.membership_state is not None and not self._membership_admissible(state, now):
            label = state.membership_state if state.membership_state != "active" else "probation"
        elif state.compatibility_error:
            label = "incompatible"
        elif state.cooldown_until > now:
            label = "circuit_open"
        elif state.capabilities is None and state.capability_probe_failed:
            label = "unreachable"
        elif state.half_open_inflight:
            label = "reconnecting"
        elif state.inflight >= capacity:
            label = "saturated"
        elif state.capabilities is None:
            label = "unknown"
        elif stale:
            label = "stale"
        else:
            label = "ready"
        capabilities = state.capabilities
        return WorkerSnapshot(
            worker_id=state.endpoint.worker_id,
            origin=state.endpoint.origin,
            state=label,
            healthy=healthy,
            inflight=state.inflight,
            capacity=capacity,
            consecutive_failures=state.consecutive_failures,
            last_error=state.last_error,
            cooldown_remaining_seconds=round(
                max(0.0, state.cooldown_until - now), 3,
            ),
            latency_ewma_ms=(
                None if state.latency_ewma_ms is None
                else round(state.latency_ewma_ms, 3)
            ),
            protocol=capabilities.protocol if capabilities else "unknown",
            version=capabilities.version if capabilities else "unknown",
            models=capabilities.models if capabilities else (),
            capabilities_stale=stale,
            cooldown_until=state.cooldown_until,
            trips=state.trips,
            probing=state.half_open_inflight or state.capability_probe_inflight,
            inference_ewma_ms=(
                None if state.inference_ewma_ms is None
                else round(state.inference_ewma_ms, 3)
            ),
            model_contexts=dict(capabilities.model_contexts) if capabilities else {},
            loaded_models=capabilities.loaded_models if capabilities else (),
            vram_bytes=capabilities.vram_bytes if capabilities else None,
            ram_bytes=capabilities.ram_bytes if capabilities else None,
            kv_cache_type=capabilities.kv_cache_type if capabilities else None,
            supports_tools=dict(capabilities.supports_tools) if capabilities else {},
            model_active_billions=dict(capabilities.model_active_billions) if capabilities else {},
            model_metadata={
                k: dict(v) for k, v in capabilities.model_metadata.items()
            } if capabilities else {},
        )

    def _encode_status_cursor(self, offset: int, principal: str) -> str:
        # A keyed opaque handle: no identity, offset, or topology is encoded
        # in the returned token, and no unbounded cursor registry is retained.
        material = f"{self._roster_generation}:{offset}:{principal}".encode("utf-8")
        digest = hmac.new(self._status_cursor_key, material, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def _decode_status_cursor(self, cursor: str | None, total: int, principal: str) -> int:
        if cursor in (None, ""):
            return 0
        if isinstance(cursor, str) and len(cursor) == 43 and cursor.isascii():
            # At most 256 candidate offsets, independent of model inventory.
            for offset in range(1, total + 1):
                if hmac.compare_digest(cursor, self._encode_status_cursor(offset, principal)):
                    return offset
        raise ValueError("invalid or stale Ollama worker status cursor")

    def validate_status_request(self, *, cursor=None, page_size=None, principal="local-open") -> tuple[int, int]:
        """Validate a cached detail request before any explicit refresh."""
        size = self._status_page_size if page_size is None else page_size
        if type(size) is not int or not 1 <= size <= _MAX_STATUS_PAGE_SIZE:
            raise ValueError("status page size must be within 1..128")
        with self._condition:
            return self._decode_status_cursor(cursor, len(self._states), principal), size

    @staticmethod
    def _status_error_category(snapshot: WorkerSnapshot) -> str:
        if not snapshot.last_error:
            # A membership worker awaiting its first controller pass has no
            # failure yet, but reporting "none" hid a lifecycle never started.
            return "membership_pending" if snapshot.state == "probation" else "none"
        error = snapshot.last_error.casefold()
        if "tls" in error or "certificate" in error or "ssl" in error:
            return "tls"
        if "timeout" in error:
            return "timeout"
        if snapshot.state == "incompatible" or "capability" in error:
            return "capability"
        if "http" in error or "protocol" in error:
            return "protocol"
        if "authoriz" in error or "forbidden" in error or "unauthorized" in error:
            return "authorization"
        if "urlerror" in error or "connection" in error or "transport" in error:
            return "transport"
        return "unknown"

    def _status_worker_record(self, snapshot: WorkerSnapshot) -> dict:
        previews = tuple(sorted({
            model[:_STATUS_MODEL_PREVIEW_LENGTH]
            for model in snapshot.models
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(?::[A-Za-z0-9._-]+)?", model)
            and self._redactor.redact(model) == model
        }))[:_STATUS_MODEL_PREVIEW_COUNT]
        return {
            "worker_id": _safe_scalar(snapshot.worker_id, limit=256),
            "origin": _safe_scalar(snapshot.origin, limit=256),
            "state": snapshot.state,
            "healthy": snapshot.healthy,
            "inflight": snapshot.inflight,
            "capacity": snapshot.capacity,
            "consecutive_failures": snapshot.consecutive_failures,
            "cooldown_remaining_seconds": snapshot.cooldown_remaining_seconds,
            "latency_ewma_ms": snapshot.latency_ewma_ms,
            "protocol": _safe_scalar(snapshot.protocol, limit=80),
            "version": snapshot.version if re.fullmatch(r"v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?", snapshot.version)
            and self._redactor.redact(snapshot.version) == snapshot.version else "unknown",
            "capabilities_stale": snapshot.capabilities_stale,
            "trips": snapshot.trips,
            "probing": snapshot.probing,
            "ewma_latency_ms": snapshot.ewma_latency_ms or 0.0,
            "inference_ewma_ms": snapshot.inference_ewma_ms,
            "error_category": OllamaWorkerPool._status_error_category(snapshot),
            "model_count": len(snapshot.models),
            "model_preview": list(previews),
            "loaded_model_count": len(snapshot.loaded_models),
            "vram_bytes": snapshot.vram_bytes,
            "ram_bytes": snapshot.ram_bytes,
            "kv_cache_type": snapshot.kv_cache_type,
        }

    @staticmethod
    def _serialized_status_bytes(payload: dict) -> int:
        return len(json.dumps(payload).encode("utf-8"))

    def _membership_status_locked(self, now: float) -> tuple[str, str]:
        """Return an authority-safe admission projection without roster details."""
        if self._external_source is None:
            return "static", "static"
        if not self._membership_roster_applied:
            return "external", "unrefreshed"
        admitted = tuple(
            state for state in self._states
            if state.membership_state not in (None, "draining")
        )
        if not admitted:
            # An authenticated empty roster is current, but it admits no remote
            # worker.  Capacity remains independently fail-closed at zero.
            return "external", "current"
        for state in admitted:
            if not (
                self._membership_admissible(state, now)
                and not state.compatibility_error
                and not (state.capabilities is None and state.capability_probe_failed)
                and state.cooldown_until <= now
                and not self._capabilities_stale(state, now)
            ):
                return "external", "stale_or_partial"
        return "external", "current"

    def _summary_locked(self, now: float) -> dict:
        """Compute bounded scalar aggregates without copying model inventories."""
        healthy = eligible = capacity = inflight = remote = refreshed = 0
        newest = None
        for state in self._states:
            remote += not _is_loopback(state.endpoint.origin)
            inflight += state.inflight
            healthy_now = (self._membership_admissible(state, now) and not state.compatibility_error
                           and not (state.capabilities is None and state.capability_probe_failed)
                           and state.cooldown_until <= now)
            healthy += healthy_now
            fresh = not self._capabilities_stale(state, now)
            eligible_now = healthy_now and fresh and not self._draining
            eligible += eligible_now
            if eligible_now:
                capacity += max(0, self._capacity(state) - state.inflight)
            if state.capabilities is not None:
                refreshed += 1
                observed = state.capabilities.observed_at
                newest = observed if newest is None else max(newest, observed)
        total = len(self._states)
        membership_mode, membership_state = self._membership_status_locked(now)
        refresh_state = (
            "not_refreshed" if membership_state == "unrefreshed" else membership_state
        ) if membership_mode == "external" else (
            "not_refreshed" if not refreshed else
            "current" if eligible == total else "stale_or_partial"
        )
        return {
            "schema_version": _STATUS_SCHEMA_VERSION,
            "roster_generation": self._roster_generation,
            "membership_mode": membership_mode,
            "membership_state": membership_state,
            "enabled": self.enabled,
            "admission": "draining" if self._draining else "accepting",
            "worker_count": total,
            "configured_worker_limit": self._max_workers,
            "worker_pool_max_workers": self._max_workers,
            "remote_worker_count": remote,
            "healthy_worker_count": healthy,
            "eligible_worker_count": eligible,
            "unhealthy_worker_count": total - healthy,
            "draining_worker_count": total if self._draining else sum(
                state.membership_state == "draining" for state in self._states),
            "membership_omitted_worker_count": self._membership_omitted,
            "available_capacity": capacity,
            "inflight": inflight,
            "refresh_state": refresh_state,
            "refresh_age_seconds": None if newest is None else round(max(0, now - newest), 3),
            "queue": {"waiting": self._waiters, "limit": self._queue_depth, "scope": "global"},
            "routing": "latency-aware-least-inflight",
            "request_placement": "whole-worker; no model sharding",
            "model_sharding": False,
            "indefinite_scale": False,
            "leases": self._lease_summary_locked(now),
            **self._gpu_admission_summary(),
        }

    def _gpu_admission_summary(self) -> dict:
        """The admission snapshot (in-memory only) unless admission is off."""
        service = self.gpu_admission
        if getattr(service, "mode", "off") == "off" or not callable(getattr(service, "snapshot", None)):
            return {}
        return {"gpu_admission": service.snapshot()}

    def summary(self) -> dict:
        """Return cached aggregate state only; never probe or construct detail."""
        with self._condition:
            now = self._clock()
            self._reconcile_leases_locked(now)
            return self._summary_locked(now)

    def pool_capacity_summary(self) -> dict[str, Any]:
        """Return cached pool-wide worker resource, residency, and context summary."""
        now = self._clock()
        with self._condition:
            self._reconcile_leases_locked(now)
            worker_rows: list[dict[str, Any]] = []
            available_models: set[str] = set()
            resident_models: set[str] = set()
            model_contexts: dict[str, int] = {}
            tool_capable_models: set[str] = set()
            total_slots = 0
            available_slots = 0
            healthy_count = 0
            vram_values: list[int] = []
            ram_values: list[int] = []
            for state in self._states:
                snap = self._snapshot(state, now)
                free_slots = max(0, snap.capacity - snap.inflight) if snap.healthy and not self._draining else 0
                if snap.healthy:
                    healthy_count += 1
                    total_slots += snap.capacity
                    available_slots += free_slots
                    available_models.update(snap.models)
                    resident_models.update(snap.loaded_models)
                    for model_name, ctx in snap.model_contexts.items():
                        prev = model_contexts.get(model_name)
                        if prev is None or ctx > prev:
                            model_contexts[model_name] = ctx
                    for model_name, supported in snap.supports_tools.items():
                        if supported:
                            tool_capable_models.add(model_name)
                    if snap.vram_bytes is not None:
                        vram_values.append(snap.vram_bytes)
                    if snap.ram_bytes is not None:
                        ram_values.append(snap.ram_bytes)
                worker_rows.append({
                    "worker_id": snap.worker_id,
                    "origin": snap.origin,
                    "state": snap.state,
                    "healthy": snap.healthy,
                    "inflight": snap.inflight,
                    "capacity": snap.capacity,
                    "free_slots": free_slots,
                    "vram_bytes": snap.vram_bytes,
                    "ram_bytes": snap.ram_bytes,
                    "kv_cache_type": snap.kv_cache_type,
                    "loaded_models": list(snap.loaded_models),
                    "model_count": len(snap.models),
                    "model_contexts": dict(snap.model_contexts),
                })
        return {
            "worker_count": len(worker_rows),
            "healthy_worker_count": healthy_count,
            "total_slots": total_slots,
            "available_slots": available_slots,
            "total_vram_bytes": sum(vram_values) if vram_values else None,
            "total_ram_bytes": sum(ram_values) if ram_values else None,
            "resident_models": sorted(resident_models),
            "available_models": sorted(available_models),
            "model_contexts": dict(sorted(model_contexts.items())),
            "tool_capable_models": sorted(tool_capable_models),
            "workers": worker_rows,
        }

    def aggregate_context_budget(self, model: str | None = None) -> dict[str, Any]:
        """Summarize context and residency budgets across healthy pool workers.

        When ``model`` is supplied, restricts per-worker context headroom,
        residency, tool capability, and MoE active-parameter metadata to
        healthy admissible workers serving that model. When omitted, returns
        the pool-wide ``pool_capacity_summary()`` with ``max_context``.
        """
        if not model or not str(model).strip():
            capacity = self.pool_capacity_summary()
            contexts = list(capacity["model_contexts"].values())
            capacity["max_context"] = max(contexts) if contexts else None
            return capacity

        target = str(model).strip()
        target_key = _model_key(target)
        now = self._clock()
        with self._condition:
            self._reconcile_leases_locked(now)
            candidates: list[tuple[tuple[int, int, float, int], _WorkerState, WorkerSnapshot]] = []
            per_worker: dict[str, dict[str, Any]] = {}
            resident_origins: list[str] = []
            contexts: list[int] = []
            kv_cache_types: dict[str, str] = {}
            tool_flags: list[bool] = []
            active_billions_val: float | None = None

            for state in self._states:
                if not (
                    self._membership_admissible(state, now)
                    and not state.compatibility_error
                    and state.cooldown_until <= now
                    and self._supports_model(state, target)
                ):
                    continue
                snap = self._snapshot(state, now)
                caps = state.capabilities
                ctx = caps.context_for(target) if caps is not None else None
                resident = (
                    target_key in {_model_key(m) for m in caps.loaded_models}
                    if caps is not None
                    else False
                )
                supports_tools_val = caps.model_supports_tools(target) if caps is not None else None
                active_b = caps.active_billions_for(target) if caps is not None else None
                if active_billions_val is None and active_b is not None:
                    active_billions_val = active_b
                if supports_tools_val is not None:
                    tool_flags.append(supports_tools_val)
                if ctx is not None:
                    contexts.append(ctx)
                if resident:
                    resident_origins.append(snap.origin)
                if snap.kv_cache_type:
                    kv_cache_types[snap.origin] = snap.kv_cache_type
                free_slots = max(0, snap.capacity - snap.inflight) if snap.healthy and not self._draining else 0
                per_worker[snap.origin] = {
                    "worker_id": snap.worker_id,
                    "healthy": snap.healthy,
                    "free_slots": free_slots,
                    "context": ctx,
                    "resident": resident,
                    "vram_bytes": snap.vram_bytes,
                    "ram_bytes": snap.ram_bytes,
                    "kv_cache_type": snap.kv_cache_type,
                    "supports_tools": supports_tools_val,
                    "active_billions": active_b,
                }
                if snap.healthy:
                    latency = state.inference_ewma_ms if state.inference_ewma_ms is not None else (
                        state.latency_ewma_ms if state.latency_ewma_ms is not None else 1000.0
                    )
                    lacks = 1 if state.known_models is not None and target_key not in state.known_models else 0
                    candidates.append((
                        (lacks, 0 if resident else 1, (state.inflight + 1) * latency, state.inflight),
                        state,
                        snap,
                    ))

        recommended_origin = min(candidates, key=lambda item: item[0])[2].origin if candidates else None
        supports_tools_agg: bool | None = None
        if tool_flags:
            supports_tools_agg = any(tool_flags)
        return {
            "model": target,
            "eligible_workers": len(per_worker),
            "max_context": max(contexts) if contexts else None,
            "min_context": min(contexts) if contexts else None,
            "recommended_worker_origin": recommended_origin,
            "resident_origins": resident_origins,
            "supports_tools": supports_tools_agg,
            "active_billions": active_billions_val,
            "kv_cache_types": kv_cache_types,
            "per_worker": per_worker,
        }

    def status(
        self, *, cursor: str | None = None, page_size: int | None = None,
        principal: str = "local-open",
    ) -> dict:
        """Return a cached administrative page without starting a probe."""
        with self._condition:
            start, selected_page_size = self.validate_status_request(
                cursor=cursor, page_size=page_size, principal=principal,
            )
            now = self._clock()
            self._reconcile_leases_locked(now)
            common = self._summary_locked(now)
            total_count = len(self._states)
            # Snapshot only this requested page. Models are immutable tuples;
            # records outside this slice are neither copied nor sanitized.
            workers = tuple(self._snapshot(state, now) for state in
                            self._states[start:start + selected_page_size])
            common.update({
                "remote_tls_required": bool(common["remote_worker_count"]) or common["membership_mode"] == "external",
                "tls_verification": (
                    "pinned-ca-exact-san"
                    if common["membership_mode"] == "external"
                    else importlib.import_module(
                        "sonder_runtime.adapters.inference.ollama_endpoint"
                    ).tls_trust_source()
                    if common["remote_worker_count"] else "not-applicable"
                ),
                "non_idempotent_failover": False,
                "metrics": dict(self._metrics),
                "probe_parallelism": self._probe_parallelism,
                "probe_batch_size": self._probe_batch_size,
                "status_page_size": self._status_page_size,
                "worker_capability_probe_parallelism": self._probe_parallelism,
                "worker_capability_probe_batch_size": self._probe_batch_size,
                "worker_status_page_size": self._status_page_size,
            })
            cursors = {end: self._encode_status_cursor(end, principal)
                       for end in range(start, min(total_count, start + selected_page_size) + 1)}

        def page_payload(records: list[dict], end: int) -> dict:
            payload = dict(common)
            payload.update({
                "page_size": selected_page_size,
                "next_cursor": (
                    cursors[end] if end < total_count else None
                ),
                "complete": end >= total_count,
                "omitted_worker_count": total_count - end,
                "workers": records,
                # Reserve the widest possible encoded value while choosing records.
                "serialized_bytes": _MAX_STATUS_SERIALIZED_BYTES,
            })
            return payload

        records: list[dict] = []
        end = start
        while end < total_count and len(records) < selected_page_size:
            candidate = records + [self._status_worker_record(workers[end - start])]
            payload = page_payload(candidate, end + 1)
            if self._serialized_status_bytes(payload) > _MAX_STATUS_SERIALIZED_BYTES:
                break
            records = candidate
            end += 1
        result = page_payload(records, end)
        for _ in range(3):
            result["serialized_bytes"] = self._serialized_status_bytes(result)
        if result["serialized_bytes"] > _MAX_STATUS_SERIALIZED_BYTES:
            raise RuntimeError("bounded Ollama worker status exceeded its byte limit")
        return result

    def operator_status_lines(self) -> tuple[str, ...]:
        """Render compact, bounded status without response bodies or prompts."""
        status = self.status()
        metrics = status["metrics"]
        lines = [
            "Ollama pool: %s; %d/%d healthy; capacity=%d; queue=%d/%d; "
            "failovers=%d; backpressure=%d" % (
                status["admission"],
                status["healthy_worker_count"],
                status["worker_count"],
                status["available_capacity"],
                status["queue"]["waiting"],
                status["queue"]["limit"],
                metrics["failovers"],
                metrics["backpressure_rejections"],
            )
        ]
        for worker in status["workers"]:
            latency = (
                "unknown" if worker["latency_ewma_ms"] is None
                else "%.1fms" % worker["latency_ewma_ms"]
            )
            lines.append(
                "  %s: %s inflight=%d/%d latency=%s models=%d version=%s%s" % (
                    worker["worker_id"],
                    worker["state"],
                    worker["inflight"],
                    worker["capacity"],
                    latency,
                    worker["model_count"],
                    worker["version"],
                    (
                        " retry=%.1fs" % worker["cooldown_remaining_seconds"]
                        if worker["cooldown_remaining_seconds"] else ""
                    ),
                )
            )
        if status["omitted_worker_count"]:
            lines.append(
                "  + %d worker(s) omitted by the configured status page limit"
                % status["omitted_worker_count"]
            )
        return tuple(lines)


def configure_typed_pool(pool: OllamaWorkerPool) -> None:
    """Bind compatibility lookup to the pool owned by the typed application."""
    import sonder_runtime.adapters.inference.ollama_endpoint as ollama_endpoint

    global _configured_pool
    if type(pool) is not OllamaWorkerPool:
        raise ValueError("exact typed application pool required")
    with ollama_endpoint._configuration_lock:
        primary = ollama_endpoint._configured_endpoint
    with _configuration_lock:
        origins = pool.configured_origins
        if (type(primary) is not str or type(_configured_workers) is not tuple
                or type(origins) is not tuple or not origins
                or any(type(origin) is not str for origin in (*origins, *_configured_workers))
                or origins != (ollama_policy.normalize(primary).rstrip("/"), *_configured_workers)):
            raise ValueError("application pool does not match typed configured origins")
        _configured_pool = (origins[0], pool)


def from_environment(primary_origin: str, environment=None) -> OllamaWorkerPool:
    """Build the pool from consented, bounded environment configuration."""
    # An origin holding "@" may carry userinfo that urlsplit reads as a host and
    # port (a password with an unencoded "/"), so it is never logged at all.
    if "@" in str(primary_origin):
        safe_primary = "<redacted>"
    else:
        try:
            primary_parts = urlsplit(ollama_policy.normalize(primary_origin))
            safe_primary = f"{primary_parts.scheme}://{primary_parts.hostname}:{primary_parts.port}"
        except ValueError:
            safe_primary = "<unparseable>"
    logger.debug(f"from_environment: primary_origin={safe_primary!r}")
    logger.info(f"building Ollama worker pool from environment, primary_origin={safe_primary!r}")
    env = os.environ if environment is None else environment
    with _configuration_lock:
        if environment is None and _configured_pool is not None:
            if ollama_policy.normalize(primary_origin).rstrip("/") != _configured_pool[0]:
                raise ValueError("primary differs from the composed typed pool")
            return _configured_pool[1]
        typed_workers = _configured_workers
        typed_allow_remote = _configured_allow_remote
        typed_trusted_origins = _configured_trusted_origins
        typed_failure = _configured_failure_threshold
        typed_cooldown = _configured_cooldown_seconds
        typed_admission = _configured_admission_timeout_ms
        typed_ttl = _configured_capability_ttl_seconds
        typed_probe = _configured_probe_timeout_ms
        typed_max_inflight = _configured_max_inflight
        typed_queue_depth = _configured_queue_depth
        typed_max_workers = _configured_max_workers
        typed_probe_parallelism = _configured_probe_parallelism
        typed_probe_batch_size = _configured_probe_batch_size
        typed_status_page_size = _configured_status_page_size
    use_typed = environment is None and typed_workers is not None and typed_allow_remote is not None
    max_workers = (
        typed_max_workers if use_typed and typed_max_workers is not None
        else _positive_int(
            env,
            "SONDER_OLLAMA_POOL_MAX_WORKERS",
            _DEFAULT_MAX_WORKERS,
            maximum=_MAX_POOL_WORKERS,
        )
    )
    probe_parallelism = (
        typed_probe_parallelism
        if use_typed and typed_probe_parallelism is not None
        else _positive_int(
            env,
            "SONDER_OLLAMA_WORKER_PROBE_PARALLELISM",
            _DEFAULT_CAPABILITY_PROBE_PARALLELISM,
            maximum=_MAX_CAPABILITY_PROBE_PARALLELISM,
        )
    )
    probe_batch_size = (
        typed_probe_batch_size
        if use_typed and typed_probe_batch_size is not None
        else _positive_int(
            env,
            "SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE",
            _DEFAULT_CAPABILITY_PROBE_BATCH_SIZE,
            maximum=_MAX_CAPABILITY_PROBE_BATCH_SIZE,
        )
    )
    status_page_size = (
        typed_status_page_size
        if use_typed and typed_status_page_size is not None
        else _positive_int(
            env,
            "SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE",
            _DEFAULT_STATUS_PAGE_SIZE,
            maximum=_MAX_STATUS_PAGE_SIZE,
        )
    )
    if use_typed:
        worker_origins = typed_workers
        allow_remote = typed_allow_remote
        trusted_origins = typed_trusted_origins or ()
        logger.debug(f"from_environment: using typed config, workers={len(worker_origins)}, allow_remote={allow_remote}")
    else:
        worker_origins = parse_worker_origins(
            env.get("SONDER_OLLAMA_WORKERS"), max_workers=max_workers,
        )
        allow_remote = str(env.get("SONDER_ALLOW_REMOTE_OLLAMA", "")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        raw_trusted = env.get("SONDER_TRUSTED_ORIGINS", "")
        trusted_origins = tuple(
            v.strip() for v in raw_trusted.replace(";", ",").split(",") if v.strip()
        )
        logger.debug(f"from_environment: using env config, workers={len(worker_origins)}, allow_remote={allow_remote}")
    cooldown = (
        typed_cooldown if use_typed and typed_cooldown is not None
        else _positive_int(
            env, "SONDER_OLLAMA_WORKER_COOLDOWN_SECONDS",
            int(_DEFAULT_COOLDOWN_SECONDS), maximum=int(_MAX_COOLDOWN_SECONDS),
        )
    )
    admission_ms = (
        typed_admission if use_typed and typed_admission is not None
        else _positive_int(
            env, "SONDER_OLLAMA_WORKER_ADMISSION_TIMEOUT_MS",
            int(_DEFAULT_ADMISSION_TIMEOUT_SECONDS * 1000),
            maximum=int(_MAX_ADMISSION_TIMEOUT_SECONDS * 1000),
        )
    )
    local_admission_timeout = _bounded_seconds(
        env,
        "SONDER_POOL_ADMISSION_TIMEOUT_SECONDS",
        _DEFAULT_LOCAL_ADMISSION_TIMEOUT_SECONDS,
        maximum=_MAX_ADMISSION_TIMEOUT_SECONDS,
    )
    probe_timeout_ms = (
        typed_probe if use_typed and typed_probe is not None
        else _positive_int(
            env, "SONDER_OLLAMA_WORKER_PROBE_TIMEOUT_MS", 2000,
            maximum=30_000,
        )
    )
    failure_threshold = (
        typed_failure if use_typed and typed_failure is not None
        else _positive_int(
            env, "SONDER_OLLAMA_WORKER_FAILURE_THRESHOLD",
            _DEFAULT_FAILURE_THRESHOLD, maximum=_MAX_FAILURE_THRESHOLD,
        )
    )
    admission = (
        gpu_admission_module.process_service() if environment is None
        else gpu_admission_module.service_for_environment(env)
    )
    recent_evidence = identity_for = None
    capability_routing = "advisory"
    if environment is None:
        # The production composition root binds persisted evidence and the
        # host identity resolver. Direct constructors remain legacy-compatible.
        from .capability_evidence import (
            capability_routing_mode,
            load_production_evidence,
            ollama_identity,
        )
        recent_evidence = load_production_evidence()
        identity_for = ollama_identity
        capability_routing = capability_routing_mode()
    else:
        # Explicit environments are used by tests and process-local callers;
        # resolve the same setting without consulting process globals.
        try:
            from .capability_evidence import capability_routing_mode
            capability_routing = capability_routing_mode(environment)
        except (ImportError, AttributeError):
            capability_routing = "advisory"
    return OllamaWorkerPool(
        primary_origin,
        worker_origins,
        allow_remote=allow_remote,
        trusted_origins=trusted_origins,
        failure_threshold=failure_threshold,
        cooldown_seconds=cooldown,
        max_workers=max_workers,
        capability_probe_parallelism=probe_parallelism,
        capability_probe_batch_size=probe_batch_size,
        capability_probe_timeout_seconds=probe_timeout_ms / 1000.0,
        status_page_size=status_page_size,
        gpu_admission=admission,
        max_inflight_per_worker=(
            typed_max_inflight if use_typed and typed_max_inflight is not None
            else _positive_int(
                env, "SONDER_OLLAMA_WORKER_MAX_INFLIGHT", _DEFAULT_MAX_INFLIGHT,
                maximum=_MAX_INFLIGHT_PER_WORKER,
            )
        ),
        queue_depth=(
            typed_queue_depth if use_typed and typed_queue_depth is not None
            else _positive_int(
                env, "SONDER_OLLAMA_WORKER_QUEUE_DEPTH", _DEFAULT_QUEUE_DEPTH,
                maximum=_MAX_QUEUE_DEPTH,
            )
        ),
        admission_timeout_seconds=admission_ms / 1000.0,
        local_admission_timeout_seconds=local_admission_timeout,
        capability_ttl_seconds=(
            typed_ttl if use_typed and typed_ttl is not None
            else _positive_int(
                env, "SONDER_OLLAMA_WORKER_CAPABILITY_TTL_SECONDS",
                int(_DEFAULT_CAPABILITY_TTL_SECONDS),
                maximum=int(_MAX_CAPABILITY_TTL_SECONDS),
            )
        ),
        capability_prober=_default_capability_prober(
            allow_remote=allow_remote, timeout=probe_timeout_ms / 1000.0,
        ),
        recent_evidence=recent_evidence,
        identity_for=identity_for,
        capability_routing=capability_routing,
        metrics=default_registry(),
    )


__all__ = [
    "OllamaWorkerPool",
    "WorkerCapabilities",
    "WorkerCapabilityUnavailable",
    "WorkerEndpoint",
    "WorkerPoolBackpressure",
    "WorkerPoolDraining",
    "WorkerPoolError",
    "WorkerPoolUnavailable",
    "WorkerSnapshot",
    "admitted",
    "admitted_get",
    "configure_typed_workers",
    "configure_typed_pool",
    "from_environment",
    "has_configured_remote_workers",
    "local_agent_admission",
    "parse_worker_origins",
    "reset_typed_workers",
    "validate_worker_origin",
]
