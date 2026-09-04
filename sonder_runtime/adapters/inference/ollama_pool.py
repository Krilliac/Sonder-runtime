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

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import http.client
import importlib
import ipaddress
import json
import math
import os
import threading
import time
from typing import Callable, Mapping
from urllib.parse import urlsplit
import urllib.error
import urllib.request

import logging

import sonder_runtime.adapters.model_inventory as model_inventory
from sonder_runtime.domain import ollama_policy
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
_MAX_WORKERS = 16
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
_configuration_lock = threading.RLock()


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


def parse_worker_origins(raw: str | None) -> tuple[str, ...]:
    """Parse a comma/semicolon-separated worker origin list."""
    values = []
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip()
        if value:
            values.append(value)
    if len(values) > _MAX_WORKERS - 1:
        raise ValueError(
            "at most %d additional Ollama workers are supported" % (_MAX_WORKERS - 1)
        )
    return tuple(values)


def _model_key(name) -> str:
    text = str(name or "").strip().casefold()
    return text[:-7] if text.endswith(":latest") else text


def _metric_label(index: int) -> str:
    return "w%d" % index if index < _MAX_METRIC_WORKERS else _METRIC_OVERFLOW_LABEL


def _host_in_trusted_origins(
    host: str, trusted_origins: tuple[str, ...],
) -> bool:
    if not trusted_origins:
        return False
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    for cidr in trusted_origins:
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


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
        if (
            parsed.scheme.casefold() != "https"
            and not _host_in_trusted_origins(parsed.hostname or "", trusted_origins)
        ):
            raise ValueError("remote worker endpoints must use https")
    result = normalized.rstrip("/")
    logger.debug(f"validated worker origin -> {result!r}")
    return result


def configure_typed_workers(
    worker_origins: tuple[str, ...],
    *,
    allow_remote: bool,
    trusted_origins: tuple[str, ...] = (),
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
    global _configured_workers, _configured_allow_remote, _configured_trusted_origins
    with _configuration_lock:
        _configured_workers = normalized
        _configured_allow_remote = allow_remote
        _configured_trusted_origins = trusted_origins


def reset_typed_workers() -> None:
    logger.info("typed Ollama worker configuration reset")
    global _configured_workers, _configured_allow_remote, _configured_trusted_origins
    with _configuration_lock:
        _configured_workers = None
        _configured_allow_remote = None
        _configured_trusted_origins = None


def has_configured_remote_workers(environment=None) -> bool:
    """Return whether typed or legacy worker configuration can leave localhost."""
    with _configuration_lock:
        typed_workers = _configured_workers
        typed_allow_remote = _configured_allow_remote
    if environment is None and typed_workers is not None and typed_allow_remote is not None:
        origins = typed_workers
    else:
        env = os.environ if environment is None else environment
        origins = parse_worker_origins(env.get("SONDER_OLLAMA_WORKERS"))
    return any(not _is_loopback(origin) for origin in origins)


@dataclass(frozen=True)
class WorkerEndpoint:
    origin: str
    worker_id: str
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
        all_origins = (primary_origin, *worker_origins)
        if len(all_origins) > _MAX_WORKERS:
            raise ValueError("at most %d Ollama workers are supported" % _MAX_WORKERS)
        states = []
        seen = set()
        for raw in all_origins:
            origin = validate_worker_origin(raw, allow_remote=allow_remote, trusted_origins=trusted_origins)
            if origin in seen:
                continue
            seen.add(origin)
            states.append(_WorkerState(
                WorkerEndpoint(origin, _worker_id(origin), _metric_label(len(states)))
            ))
        if not states:
            raise ValueError("at least one Ollama worker is required")
        logger.debug(
            f"OllamaWorkerPool.__init__: workers={len(states)}, "
            f"origins={[s.endpoint.origin for s in states]}, "
            f"failure_threshold={failure_threshold}, cooldown={cooldown_seconds}s, "
            f"max_inflight={max_inflight_per_worker}, queue_depth={queue_depth}, "
            f"admission_timeout={admission_timeout_seconds}s, capability_ttl={capability_ttl_seconds}s"
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
        self._capability_prober = capability_prober
        self._clock = time_fn or clock
        self._cursor = 0
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
        self._redactor = redactor or Redactor()

    @property
    def enabled(self) -> bool:
        return len(self._states) > 1

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
                    worker=state.endpoint.metric_label, state="open"
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
                    worker=state.endpoint.metric_label, state="closed"
                )

    def refresh_capabilities(
        self, *, force: bool = False,
    ) -> tuple[WorkerSnapshot, ...]:
        """Probe stale workers concurrently and retain deterministic state order.

        Circuit-open workers are not probed before their retry deadline unless
        ``force`` is explicitly requested by an operator-facing caller.
        """
        logger.debug(f"refresh_capabilities called, force={force}")
        if self._capability_prober is None:
            logger.debug("no capability prober configured, skipping refresh")
            return self.snapshots()
        if not self._probe_lock.acquire(blocking=False):
            logger.debug("probe lock contended, skipping refresh")
            return self.snapshots()
        try:
            now = self._clock()
            with self._condition:
                candidates = [
                    state for state in self._states
                    if (force or self._capabilities_stale(state, now))
                    and (force or state.cooldown_until <= now)
                    and not state.half_open_inflight
                ]
            if not candidates:
                logger.debug("no stale/eligible workers to probe")
                return self.snapshots()

            def run(state: _WorkerState):
                started = self._clock()
                try:
                    payload = self._capability_prober(state.endpoint.origin)
                    elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
                    return payload, elapsed_ms, None
                except Exception as error:
                    return None, 0.0, error

            logger.debug(f"probing {len(candidates)} candidate workers: {[s.endpoint.worker_id for s in candidates]}")
            logger.info(f"probing capabilities on {len(candidates)} worker(s)")
            workers = min(4, len(candidates))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(run, state) for state in candidates]
                outcomes = [future.result() for future in futures]

            with self._condition:
                for state, (payload, measured_ms, error) in zip(candidates, outcomes):
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
                    state.compatibility_error = ""
                    state.capability_probe_failed = False
                    self._record_success(state, latency_ms)
                self._condition.notify_all()
            return self.snapshots()
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
                    state.inflight += 1
                    if state.consecutive_failures >= self._failure_threshold:
                        state.half_open_inflight = True
                    if queued:
                        self._waiters -= 1
                    return state

                remaining_states = [
                    state for state in self._states
                    if state.endpoint.worker_id not in excluded
                    and not state.compatibility_error
                    and self._supports_model(state, model)
                ]
                if not remaining_states:
                    if queued:
                        self._waiters -= 1
                    known = all(state.capabilities is not None for state in self._states)
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
                    worker=state.endpoint.metric_label,
                    result=result,
                    elapsed_seconds=max(0.0, latency_ms / 1000.0),
                )
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
            snapshots = []
            for state in self._states:
                stale = self._capabilities_stale(state, now)
                healthy = (
                    not state.compatibility_error
                    and not (
                        state.capabilities is None
                        and state.capability_probe_failed
                    )
                    and state.cooldown_until <= now
                )
                capacity = self._capacity(state)
                if self._draining:
                    label = "draining" if state.inflight else "drained"
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
                snapshots.append(WorkerSnapshot(
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
                ))
            return tuple(snapshots)

    def status(self) -> dict:
        workers = self.snapshots()
        with self._condition:
            metrics = dict(self._metrics)
            waiters = self._waiters
            draining = self._draining
        healthy_count = sum(1 for w in workers if w.healthy)
        total_count = len(workers)
        if healthy_count < total_count and not draining:
            logger.warning(
                f"pool running with {healthy_count}/{total_count} healthy "
                f"workers; unhealthy: {[w.worker_id for w in workers if not w.healthy]}"
            )
        return {
            "enabled": self.enabled,
            "admission": "draining" if draining else "accepting",
            "worker_count": len(workers),
            "remote_worker_count": sum(
                1 for state in self._states
                if not _is_loopback(state.endpoint.origin)
            ),
            "healthy_worker_count": sum(1 for worker in workers if worker.healthy),
            "available_capacity": sum(
                max(0, worker.capacity - worker.inflight)
                for worker in workers
                if not draining
                and worker.healthy
                and worker.state not in {"incompatible", "circuit_open"}
            ),
            "remote_tls_required": self.has_remote_workers,
            "tls_verification": (
                "system-trust-store" if self.has_remote_workers else "not-applicable"
            ),
            "non_idempotent_failover": False,
            "queue": {"waiting": waiters, "limit": self._queue_depth},
            "routing": "latency-aware-least-inflight",
            "metrics": metrics,
            "workers": [snapshot.to_dict() for snapshot in workers],
        }

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
                    len(worker["models"]),
                    worker["version"],
                    (
                        " retry=%.1fs" % worker["cooldown_remaining_seconds"]
                        if worker["cooldown_remaining_seconds"] else ""
                    ),
                )
            )
        return tuple(lines)


def from_environment(primary_origin: str, environment=None) -> OllamaWorkerPool:
    """Build the pool from consented, bounded environment configuration."""
    logger.debug(f"from_environment: primary_origin={primary_origin!r}")
    logger.info(f"building Ollama worker pool from environment, primary_origin={primary_origin!r}")
    env = os.environ if environment is None else environment
    with _configuration_lock:
        typed_workers = _configured_workers
        typed_allow_remote = _configured_allow_remote
        typed_trusted_origins = _configured_trusted_origins
    if environment is None and typed_workers is not None and typed_allow_remote is not None:
        worker_origins = typed_workers
        allow_remote = typed_allow_remote
        trusted_origins = typed_trusted_origins or ()
        logger.debug(f"from_environment: using typed config, workers={len(worker_origins)}, allow_remote={allow_remote}")
    else:
        worker_origins = parse_worker_origins(env.get("SONDER_OLLAMA_WORKERS"))
        allow_remote = str(env.get("SONDER_ALLOW_REMOTE_OLLAMA", "")).strip().lower() in {
            "1", "true", "yes", "on",
        }
        raw_trusted = env.get("SONDER_TRUSTED_ORIGINS", "")
        trusted_origins = tuple(
            v.strip() for v in raw_trusted.replace(";", ",").split(",") if v.strip()
        )
        logger.debug(f"from_environment: using env config, workers={len(worker_origins)}, allow_remote={allow_remote}")
    cooldown = _positive_int(
        env,
        "SONDER_OLLAMA_WORKER_COOLDOWN_SECONDS",
        int(_DEFAULT_COOLDOWN_SECONDS),
        maximum=int(_MAX_COOLDOWN_SECONDS),
    )
    admission_ms = _positive_int(
        env,
        "SONDER_OLLAMA_WORKER_ADMISSION_TIMEOUT_MS",
        int(_DEFAULT_ADMISSION_TIMEOUT_SECONDS * 1000),
        maximum=int(_MAX_ADMISSION_TIMEOUT_SECONDS * 1000),
    )
    probe_timeout_ms = _positive_int(
        env, "SONDER_OLLAMA_WORKER_PROBE_TIMEOUT_MS", 2000,
        maximum=30_000,
    )
    return OllamaWorkerPool(
        primary_origin,
        worker_origins,
        allow_remote=allow_remote,
        trusted_origins=trusted_origins,
        failure_threshold=_positive_int(
            env,
            "SONDER_OLLAMA_WORKER_FAILURE_THRESHOLD",
            _DEFAULT_FAILURE_THRESHOLD,
            maximum=_MAX_FAILURE_THRESHOLD,
        ),
        cooldown_seconds=cooldown,
        max_inflight_per_worker=_positive_int(
            env, "SONDER_OLLAMA_WORKER_MAX_INFLIGHT", _DEFAULT_MAX_INFLIGHT,
            maximum=_MAX_INFLIGHT_PER_WORKER,
        ),
        queue_depth=_positive_int(
            env, "SONDER_OLLAMA_WORKER_QUEUE_DEPTH", _DEFAULT_QUEUE_DEPTH,
            maximum=_MAX_QUEUE_DEPTH,
        ),
        admission_timeout_seconds=admission_ms / 1000.0,
        capability_ttl_seconds=_positive_int(
            env,
            "SONDER_OLLAMA_WORKER_CAPABILITY_TTL_SECONDS",
            int(_DEFAULT_CAPABILITY_TTL_SECONDS),
            maximum=int(_MAX_CAPABILITY_TTL_SECONDS),
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
    "from_environment",
    "has_configured_remote_workers",
    "parse_worker_origins",
    "reset_typed_workers",
    "validate_worker_origin",
]
