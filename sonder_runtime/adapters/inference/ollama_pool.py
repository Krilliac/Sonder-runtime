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

from sonder_runtime.platform.runtime_threads import ThreadPoolExecutor as owned_runtime_pool

import base64
import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
import http.client
import importlib
import ipaddress
import json
import math
import os
import re
import threading
import time
from typing import Callable, Mapping
from urllib.parse import urlsplit
import urllib.error
import urllib.request

import logging

import sonder_runtime.adapters.model_inventory as model_inventory
from sonder_runtime.domain import ollama_policy
from sonder_runtime.domain.inference_membership import (
    CapabilityEvidence, MembershipHighWater, MembershipReconciliation,
    MembershipRoster, MembershipSnapshot, WorkerAdvertisement,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.platform.logging import Redactor
from sonder_runtime.platform.metrics import MetricsRegistry, default_registry

logger = logging.getLogger(__name__)


_FAILOVER_HTTP_CODES = frozenset({502, 503, 504})
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_SECONDS = 30.0
_DEFAULT_MAX_INFLIGHT = 1
_DEFAULT_QUEUE_DEPTH = 32
_DEFAULT_ADMISSION_TIMEOUT_SECONDS = 1.0
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
_MAX_FAILURE_THRESHOLD = 100
_MAX_COOLDOWN_SECONDS = 3600.0
_MAX_CAPABILITY_TTL_SECONDS = 86_400.0
_PROBE_RESPONSE_LIMIT = 1_048_576
_PROTOCOL = "ollama-http-v1"
_MAX_METRIC_WORKERS = 16
_METRIC_OVERFLOW_LABEL = "overflow"
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
    """Base for privacy-safe pool admission and availability failures."""


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


def _safe_error(error: BaseException) -> str:
    """Return bounded single-line diagnostics without response bodies."""
    if isinstance(error, urllib.error.HTTPError):
        return "HTTPError: HTTP %d" % int(error.code or 0)
    if isinstance(error, urllib.error.URLError):
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            return "URLError: transport timed out"
        return "URLError: transport unavailable"
    text = str(getattr(error, "reason", error) or type(error).__name__)
    text = " ".join(text.replace("\x00", "").split())
    return "%s: %s" % (type(error).__name__, text[:200])


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
    logger.debug(f"validating worker origin={origin!r}, allow_remote={allow_remote}, trusted_origins={trusted_origins!r}")
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
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(
            "worker endpoint must be an origin without a path, query, or fragment"
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
    logger.debug(f"configuring typed workers: count={len(worker_origins)}, allow_remote={allow_remote}, trusted_origins={trusted_origins!r}")
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
    latency_ewma_ms: float | None = None
    capabilities: WorkerCapabilities | None = None
    compatibility_error: str = ""
    capability_probe_failed: bool = False
    trips: int = 0
    known_models: frozenset[str] | None = None
    advertisement: WorkerAdvertisement | None = None
    membership_state: str | None = None
    membership_expires_at: datetime | None = None
    membership_evidence: CapabilityEvidence | None = None
    capability_checked_at: datetime | None = None


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


def _default_capability_prober(*, allow_remote: bool, timeout: float = 2.0):
    """Build a bounded prober using the same no-proxy/no-redirect transport."""
    ollama_endpoint = importlib.import_module(
        "sonder_runtime.adapters.inference.ollama_endpoint"
    )

    def read(origin: str, path: str) -> dict:
        request = urllib.request.Request(origin + path, method="GET")
        with ollama_endpoint.open_url(
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
        return {
            "protocol": _PROTOCOL,
            "version": str(version.get("version") or "unknown")[:80],
            "models": _model_names(tags),
        }

    return probe


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
        capability_ttl_seconds: float = _DEFAULT_CAPABILITY_TTL_SECONDS,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        capability_probe_parallelism: int = _DEFAULT_CAPABILITY_PROBE_PARALLELISM,
        capability_probe_batch_size: int = _DEFAULT_CAPABILITY_PROBE_BATCH_SIZE,
        status_page_size: int = _DEFAULT_STATUS_PAGE_SIZE,
        capability_prober: Callable[[str], object] | None = None,
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
        if not 1 <= capability_ttl_seconds <= _MAX_CAPABILITY_TTL_SECONDS:
            raise ValueError("capability TTL must be within 1..86400 seconds")
        if not 1 <= max_workers <= _MAX_POOL_WORKERS:
            raise ValueError("max workers must be within 1..256")
        if not 1 <= capability_probe_parallelism <= _MAX_CAPABILITY_PROBE_PARALLELISM:
            raise ValueError("capability probe parallelism must be within 1..8")
        if not 1 <= capability_probe_batch_size <= _MAX_CAPABILITY_PROBE_BATCH_SIZE:
            raise ValueError("capability probe batch size must be within 1..128")
        if not 1 <= status_page_size <= _MAX_STATUS_PAGE_SIZE:
            raise ValueError("status page size must be within 1..128")
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
            f"max_workers={max_workers}, probe_parallelism={capability_probe_parallelism}, "
            f"probe_batch_size={capability_probe_batch_size}, status_page_size={status_page_size}"
        )
        logger.info(
            f"Ollama worker pool initialized with {len(states)} worker(s), "
            f"max_inflight={max_inflight_per_worker}, queue_depth={queue_depth}"
        )
        self._states = states
        self._failure_threshold = int(failure_threshold)
        self._cooldown_seconds = float(cooldown_seconds)
        self._max_inflight = int(max_inflight_per_worker)
        self._queue_depth = int(queue_depth)
        self._admission_timeout = float(admission_timeout_seconds)
        self._capability_ttl = float(capability_ttl_seconds)
        self._max_workers = int(max_workers)
        self._probe_parallelism = int(capability_probe_parallelism)
        self._probe_batch_size = int(capability_probe_batch_size)
        self._status_page_size = int(status_page_size)
        self._capability_prober = capability_prober
        self._clock = time_fn or clock
        self._cursor = 0
        self._probe_cursor = 0
        self._roster_generation = 1
        self._status_cursor_key = os.urandom(32)
        self._waiters = 0
        self._draining = False
        self._condition = threading.Condition(threading.RLock())
        self._probe_lock = threading.Lock()
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
        self._configured_remote_origins = frozenset(
            origin for origin in normalized_origins if not _is_loopback(origin))
        self._local_worker_count = len(states) - len(self._configured_remote_origins)
        self._membership_omitted = 0

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

    def validate_membership_snapshot(self, snapshot: MembershipSnapshot) -> None:
        if type(snapshot) is not MembershipSnapshot or self._membership_authority is None:
            raise ValueError("verified configured membership snapshot required")
        if (snapshot.cluster_id, snapshot.issuer_id) != self._membership_authority:
            raise ValueError("membership authority differs from static configuration")
        if len(snapshot.workers) > self.membership_limit:
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
                    if state.inflight:
                        retained.append(state)
            omitted = result.omitted_worker_count
            for key, member in desired.items():
                state = existing.get(key)
                if state is None:
                    if len(retained) + len(existing) >= self._max_workers:
                        omitted += 1
                        continue
                    worker = member.advertisement
                    state = _WorkerState(WorkerEndpoint(worker.origin, worker.worker_id),
                                         advertisement=worker)
                else:
                    existing.pop(key)
                    if key in new_admissions:
                        # A lease renewed after expiry must obtain new evidence;
                        # a still-fresh capability cache predates this admission.
                        state.capabilities = None
                        state.known_models = None
                state.membership_state = member.lifecycle_state
                state.membership_expires_at = result.roster.snapshot.expires_at
                state.membership_evidence = member.evidence
                retained.append(state)
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

    def _prune_drained(self) -> None:
        remaining = [state for state in self._states
                     if state.membership_state != "draining" or state.inflight]
        if len(remaining) != len(self._states):
            self._states = remaining
            self._roster_generation += 1

    def _membership_admissible(self, state: _WorkerState, now: float) -> bool:
        if state.membership_state is None:
            return True
        wall_now = self._membership_clock()
        return (state.membership_state == "active" and state.membership_expires_at is not None
                and wall_now < state.membership_expires_at and state.membership_evidence is not None
                and state.membership_evidence.checked_at <= wall_now < state.membership_evidence.expires_at
                and not self._capabilities_stale(state, now) and not state.capability_probe_failed)

    def refresh_membership_capabilities(self) -> None:
        self.refresh_capabilities(_membership=True)

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
    def origins(self) -> tuple[str, ...]:
        return tuple(state.endpoint.origin for state in self._states)

    @staticmethod
    def _retryable(error: BaseException) -> bool:
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

    def _normalize_capabilities(
        self, payload: object, *, now: float, measured_ms: float,
    ) -> tuple[WorkerCapabilities, float]:
        if isinstance(payload, WorkerCapabilities):
            protocol = payload.protocol
            version = payload.version
            models = payload.models
            advertised = payload.effective_max_inflight
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
        ), round(measured_ms, 3)

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

    def _record_success(self, state: _WorkerState, latency_ms: float) -> None:
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
        if state.latency_ewma_ms is None:
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
        if not self._probe_lock.acquire(blocking=False):
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
                    if state.membership_state is not None and (
                        state.membership_state in ("draining", "expired")
                        or state.membership_expires_at is None
                        or self._membership_clock() >= state.membership_expires_at
                    ):
                        continue
                    if _membership and state.membership_state is None:
                        continue
                    if not (
                        (force or self._capabilities_stale(state, now))
                        and (force or state.cooldown_until <= now)
                        and not state.half_open_inflight
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
                started = self._clock()
                try:
                    payload = self._capability_prober(state.endpoint.origin)
                    elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
                    return payload, elapsed_ms, None
                except Exception as error:
                    return None, 0.0, error

            logger.debug(
                f"probing {len(candidates)} candidate workers: "
                f"{[s.endpoint.worker_id for s in candidates]}"
            )
            logger.info(f"probing capabilities on {len(candidates)} worker(s)")
            workers = min(self._probe_parallelism, len(candidates))
            with owned_runtime_pool(max_workers=workers) as executor:
                futures = [executor.submit(run, state) for state in candidates]
                outcomes = [future.result() for future in futures]

            with self._condition:
                for state, (payload, measured_ms, error) in zip(candidates, outcomes):
                    if state.membership_state == "draining" or not any(current is state for current in self._states):
                        continue
                    self._metrics["capability_probes"] += 1
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
            self._probe_lock.release()

    def _supports_model(self, state: _WorkerState, model: str | None) -> bool:
        if not model:
            return True
        if state.capabilities is None:
            return not state.capability_probe_failed
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

    def _capacity(self, state: _WorkerState) -> int:
        if state.capabilities is None:
            return self._max_inflight
        return state.capabilities.effective_max_inflight

    def _choose(
        self, *, model: str | None, excluded: set[str], now: float,
    ) -> _WorkerState | None:
        candidates = [
            state for state in self._states
            if state.endpoint.worker_id not in excluded
            and self._membership_admissible(state, now)
            and not state.compatibility_error
            and self._supports_model(state, model)
            and state.cooldown_until <= now
            and not state.half_open_inflight
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

        def score(state: _WorkerState):
            latency = state.latency_ewma_ms
            if latency is None:
                latency = 1000.0
            lacks_model = (
                1 if model and state.known_models is not None
                and _model_key(model) not in state.known_models else 0
            )
            return (lacks_model, (state.inflight + 1) * latency, state.inflight)

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
    ) -> _WorkerState:
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
                state = self._choose(model=model, excluded=excluded, now=now)
                if state is not None:
                    if self._metrics_observer is not None:
                        self._worker_metric_label(state)
                    state.inflight += 1
                    if state.consecutive_failures >= self._failure_threshold:
                        state.half_open_inflight = True
                    if queued:
                        self._waiters -= 1
                    return state

                remaining_states = [
                    state for state in self._states
                    if state.endpoint.worker_id not in excluded
                    and self._membership_admissible(state, now)
                    and not state.compatibility_error
                    and self._supports_model(state, model)
                ]
                if not remaining_states:
                    if queued:
                        self._waiters -= 1
                    available = [state for state in self._states if self._membership_admissible(state, now)]
                    known = bool(available) and all(state.capabilities is not None for state in available)
                    if model and known:
                        raise WorkerCapabilityUnavailable(
                            "no Ollama worker advertises model %r" % model
                        )
                    raise WorkerPoolUnavailable("no Ollama worker is available")

                saturated = any(
                    state.cooldown_until <= now
                    and state.inflight >= self._capacity(state)
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
                        % retry_after
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
                    self._waiters -= 1
                    self._metrics["backpressure_rejections"] += 1
                    logger.warning(
                        f"admission timeout after {admission_timeout:.3f}s "
                        f"waiting for worker capacity, model={model!r}, "
                        f"waiters={self._waiters}/{self._queue_depth}"
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
    ) -> None:
        if latency_ms is None:
            latency_ms = max(0.0, float(elapsed or 0.0) * 1000.0)
        with self._condition:
            state.inflight = max(0, state.inflight - 1)
            state.half_open_inflight = False
            if error is None:
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

    def request(
        self,
        sender: Callable[[str], object],
        *,
        model: str | None = None,
        admission_timeout_seconds: float | None = None,
        idempotent: bool = False,
    ):
        """Admit and send one logical request with pre-response failover only."""
        model = str(model or "").strip() or None
        logger.debug(f"pool.request: model={model!r}, idempotent={idempotent}")
        with self._condition:
            self._metrics["logical_requests"] += 1
        if model:
            self.refresh_capabilities()
        admission_timeout = (
            self._admission_timeout
            if admission_timeout_seconds is None
            else max(0.0, float(admission_timeout_seconds))
        )
        admission_deadline = time.monotonic() + admission_timeout
        attempted: set[str] = set()
        last_error = None
        while True:
            if len(attempted) >= self._max_workers:
                # Reconciliation cannot grow one logical request's retry set.
                raise last_error or WorkerPoolUnavailable("Ollama worker attempt limit reached")
            try:
                state = self._acquire(
                    model=model,
                    excluded=attempted,
                    admission_timeout=max(
                        0.0, admission_deadline - time.monotonic(),
                    ),
                )
            except WorkerPoolUnavailable:
                if last_error is not None:
                    logger.error(
                        f"all attempted workers failed for model={model!r}, "
                        f"attempted={attempted}, last_error={_safe_error(last_error)!r}"
                    )
                    raise last_error
                raise
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
            started = self._clock()
            try:
                result = sender(state.endpoint.origin)
            except Exception as error:
                latency_ms = max(0.0, (self._clock() - started) * 1000.0)
                retryable = self._retryable(error)
                logger.debug(
                    f"pool.request: error from {state.endpoint.worker_id} after {latency_ms:.1f}ms, "
                    f"retryable={retryable}: {_safe_error(error)}"
                )
                self._finish(
                    state, error=error, latency_ms=latency_ms,
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
            self._finish(state, error=None, latency_ms=latency_ms)
            return result

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
            probing=state.half_open_inflight,
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
            return "none"
        error = snapshot.last_error.casefold()
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
            "error_category": OllamaWorkerPool._status_error_category(snapshot),
            "model_count": len(snapshot.models),
            "model_preview": list(previews),
        }

    @staticmethod
    def _serialized_status_bytes(payload: dict) -> int:
        return len(json.dumps(payload).encode("utf-8"))

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
        refresh_state = ("not_refreshed" if not refreshed else
                         "current" if eligible == total else "stale_or_partial")
        return {
            "schema_version": _STATUS_SCHEMA_VERSION,
            "roster_generation": self._roster_generation,
            "membership_mode": "static",
            "membership_state": "static",
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
        }

    def summary(self) -> dict:
        """Return cached aggregate state only; never probe or construct detail."""
        with self._condition:
            return self._summary_locked(self._clock())

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
            common = self._summary_locked(now)
            total_count = len(self._states)
            # Snapshot only this requested page. Models are immutable tuples;
            # records outside this slice are neither copied nor sanitized.
            workers = tuple(self._snapshot(state, now) for state in
                            self._states[start:start + selected_page_size])
            common.update({
                "remote_tls_required": bool(common["remote_worker_count"]),
                "tls_verification": "system-trust-store" if common["remote_worker_count"] else "not-applicable",
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
    global _configured_pool
    if type(pool) is not OllamaWorkerPool:
        raise ValueError("exact typed application pool required")
    with _configuration_lock:
        if _configured_workers is None or tuple(pool.origins[1:]) != _configured_workers:
            raise ValueError("application pool does not match typed workers")
        _configured_pool = (pool.origins[0], pool)


def from_environment(primary_origin: str, environment=None) -> OllamaWorkerPool:
    """Build the pool from consented, bounded environment configuration."""
    logger.debug(f"from_environment: primary_origin={primary_origin!r}")
    logger.info(f"building Ollama worker pool from environment, primary_origin={primary_origin!r}")
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
        status_page_size=status_page_size,
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
    "configure_typed_workers",
    "configure_typed_pool",
    "from_environment",
    "has_configured_remote_workers",
    "parse_worker_origins",
    "reset_typed_workers",
    "validate_worker_origin",
]
