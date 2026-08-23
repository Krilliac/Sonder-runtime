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

from sonder_runtime.adapters import model_inventory
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain import ollama_policy
from sonder_runtime.platform.logging import Redactor
from sonder_runtime.platform.metrics import MetricsRegistry, default_registry


_FAILOVER_HTTP_CODES = frozenset({502, 503, 504})
_DEFAULT_FAILURE_THRESHOLD = 3
_DEFAULT_COOLDOWN_SECONDS = 30.0
# Repeated circuit trips double the cooldown up to this multiple of the base,
# so a host that stays down is probed progressively less often.
_COOLDOWN_MAX_MULTIPLIER = 8
# Exponential moving average weight for per-worker success latency.
_LATENCY_EWMA_ALPHA = 0.3
# Metric labels are assigned "w0".."w{_MAX_METRIC_WORKERS - 1}" in
# registration order; any worker beyond the cap shares the "overflow" label so
# an operator-supplied worker list can never grow Prometheus cardinality
# without bound (see platform/metrics.py's bounded-label-set contract).
_MAX_METRIC_WORKERS = 16
_METRIC_OVERFLOW_LABEL = "overflow"
_configured_workers: tuple[str, ...] | None = None
_configured_allow_remote: bool | None = None
_configuration_lock = threading.RLock()


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


def validate_worker_origin(origin: str, *, allow_remote: bool) -> str:
    """Normalize one worker origin under the Ollama trust policy."""
    normalized = ollama_policy.normalize(origin)
    parsed = urlsplit(normalized)
    if parsed.scheme.casefold() not in {"http", "https"}:
        raise ValueError("worker endpoint must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("worker endpoint must not contain inline credentials")
    if not parsed.hostname or port is None:
        raise ValueError("worker endpoint must include a host and explicit port")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("worker endpoint must be an origin without a path, query, or fragment")
    if not _is_loopback(normalized):
        if not allow_remote:
            raise ValueError(
                "remote worker endpoints require SONDER_ALLOW_REMOTE_OLLAMA=1"
            )
        if parsed.scheme.casefold() != "https":
            raise ValueError("remote worker endpoints must use https")
    return normalized.rstrip("/")


def configure_typed_workers(
    worker_origins: tuple[str, ...], *, allow_remote: bool,
) -> None:
    """Bind validated typed worker configuration before legacy composition.

    The canonical serve path deliberately avoids round-tripping typed startup
    authority through mutable environment variables.  Keep the same contract
    for worker endpoints so a validated TOML worker list cannot be silently
    ignored when the legacy model runtime is imported lazily.
    """
    if not isinstance(allow_remote, bool):
        raise TypeError("allow_remote must be a boolean")
    normalized = tuple(
        validate_worker_origin(origin, allow_remote=allow_remote)
        for origin in tuple(worker_origins)
    )
    global _configured_workers, _configured_allow_remote
    with _configuration_lock:
        _configured_workers = normalized
        _configured_allow_remote = allow_remote


def reset_typed_workers() -> None:
    global _configured_workers, _configured_allow_remote
    with _configuration_lock:
        _configured_workers = None
        _configured_allow_remote = None


@dataclass(frozen=True)
class WorkerEndpoint:
    origin: str
    worker_id: str
    metric_label: str


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
    cooldown_until: float
    trips: int = 0
    probing: bool = False
    ewma_latency_ms: float = 0.0

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
            "cooldown_until": self.cooldown_until,
            "trips": self.trips,
            "probing": self.probing,
            "ewma_latency_ms": self.ewma_latency_ms,
        }


@dataclass
class _WorkerState:
    endpoint: WorkerEndpoint
    inflight: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    cooldown_until: float = 0.0
    # Circuit trips since the last success; nonzero after cooldown expiry
    # marks the half-open state (a single trial request at a time).
    trips: int = 0
    # Exponential moving average of successful request latency in seconds;
    # 0.0 means unmeasured, which deliberately sorts first so a fresh worker
    # receives traffic and gets measured.
    ewma_latency: float = 0.0
    # Case-normalized model inventory advertised by this worker, or None when
    # never recorded.  Experimental affinity seam: see ``note_models``.
    known_models: frozenset | None = None


def _model_key(name) -> str:
    """Case-normalized model comparison key; ``:latest`` is implicit."""
    text = str(name or "").strip().casefold()
    if text.endswith(":latest"):
        text = text[: -len(":latest")]
    return text


def _worker_id(origin: str) -> str:
    parsed = urlsplit(origin)
    host = parsed.hostname or "worker"
    port = parsed.port or 443
    return "%s:%s" % (host, port)


def _metric_label(index: int) -> str:
    return "w%d" % index if index < _MAX_METRIC_WORKERS else _METRIC_OVERFLOW_LABEL


class OllamaWorkerPool:
    """Thread-safe, model-aware scheduler for independent Ollama hosts."""

    def __init__(
        self,
        primary_origin: str,
        worker_origins: tuple[str, ...] = (),
        *,
        allow_remote: bool = False,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        time_fn: Callable[[], float] = time.monotonic,
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
            origin = validate_worker_origin(raw, allow_remote=allow_remote)
            if origin in seen:
                continue
            seen.add(origin)
            endpoint = WorkerEndpoint(
                origin, _worker_id(origin), _metric_label(len(states))
            )
            states.append(_WorkerState(endpoint))
        if not states:
            raise ValueError("at least one Ollama worker is required")
        self._states = states
        self._failure_threshold = int(failure_threshold)
        self._cooldown_seconds = float(cooldown_seconds)
        self._cooldown_max_seconds = float(cooldown_seconds) * _COOLDOWN_MAX_MULTIPLIER
        self._time = time_fn
        self._cursor = 0
        self._lock = threading.RLock()
        self._metrics = metrics if metrics is not None else default_registry()
        self._redactor = redactor if redactor is not None else Redactor()

    @property
    def enabled(self) -> bool:
        return len(self._states) > 1

    @property
    def has_remote_workers(self) -> bool:
        return any(not _is_loopback(state.endpoint.origin) for state in self._states)

    @property
    def origins(self) -> tuple[str, ...]:
        return tuple(state.endpoint.origin for state in self._states)

    def _ordered(self, model: str | None = None) -> list[_WorkerState]:
        wanted = _model_key(model)
        now = self._time()
        with self._lock:
            ready = []
            for state in self._states:
                if state.cooldown_until > now:
                    continue
                if state.trips and state.inflight > 0:
                    # Half-open: an expired cooldown admits one trial request
                    # at a time until a success closes the circuit again.
                    continue
                ready.append(state)
            if not ready:
                ready = [min(self._states, key=lambda item: item.cooldown_until)]
            start = self._cursor % len(ready)
            self._cursor += 1
            rotated = ready[start:] + ready[:start]

            def rank(item: _WorkerState):
                lacks_model = (
                    1
                    if wanted and item.known_models is not None
                    and wanted not in item.known_models
                    else 0
                )
                return (lacks_model, item.inflight, item.ewma_latency)

            return sorted(rotated, key=rank)

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        if isinstance(error, ModelCallError):
            # A classified failure means a worker answered and the response
            # was judged (protocol, configuration, scope...). Replaying it on
            # another host would violate the no-replay contract — except an
            # upstream 502/503/504, where no model response was produced.
            # ModelCallError subclasses URLError, so without this branch every
            # classified failure would look like a transport failure below.
            return error.status in _FAILOVER_HTTP_CODES
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

    def _finish(
        self,
        state: _WorkerState,
        error: BaseException | None,
        elapsed: float | None = None,
        *,
        count_failure: bool = True,
    ) -> None:
        with self._lock:
            state.inflight = max(0, state.inflight - 1)
            label = state.endpoint.metric_label
            if error is None:
                recovered = state.consecutive_failures > 0
                state.consecutive_failures = 0
                state.trips = 0
                state.last_error = ""
                state.cooldown_until = 0.0
                if elapsed is not None and elapsed >= 0:
                    if state.ewma_latency <= 0:
                        state.ewma_latency = float(elapsed)
                    else:
                        state.ewma_latency += _LATENCY_EWMA_ALPHA * (
                            float(elapsed) - state.ewma_latency
                        )
                self._metrics.observe_ollama_worker_request(
                    worker=label, result="ok", elapsed_seconds=elapsed or 0.0,
                )
                if recovered:
                    self._metrics.observe_ollama_worker_circuit(
                        worker=label, state="closed",
                    )
                return
            # Transport exception text can contain internal hostnames, proxy
            # paths, or credential-shaped response fragments.  Status needs a
            # stable category, not the provider's free-form detail.
            state.last_error = type(error).__name__
            self._metrics.observe_ollama_worker_request(
                worker=label, result="error", elapsed_seconds=elapsed or 0.0,
            )
            if not count_failure:
                return
            state.consecutive_failures += 1
            if state.consecutive_failures >= self._failure_threshold:
                now = self._time()
                already_open = state.cooldown_until > now
                state.trips += 1
                backoff = min(
                    self._cooldown_seconds * (2 ** (state.trips - 1)),
                    self._cooldown_max_seconds,
                )
                state.cooldown_until = now + backoff
                if not already_open:
                    self._metrics.observe_ollama_worker_circuit(
                        worker=label, state="open",
                    )

    def note_models(self, origin_or_id: str, model_names) -> bool:
        """Record a worker's advertised model inventory (experimental seam).

        Inventory only *orders* selection — a worker whose recorded inventory
        lacks the requested model sorts last but is never excluded, because a
        recorded list may be stale and a worker can pull a model on demand.
        Returns whether a matching worker was found.
        """
        target = str(origin_or_id or "").strip().rstrip("/")
        names = frozenset(
            key for key in (_model_key(name) for name in (model_names or ())) if key
        )
        with self._lock:
            for state in self._states:
                if target in (state.endpoint.origin, state.endpoint.worker_id):
                    state.known_models = names
                    return True
        return False

    def refresh_inventory(self, fetch_tags: Callable[[str], object]) -> dict:
        """Record every worker's advertised model inventory (experimental).

        ``fetch_tags(origin)`` must return that origin's parsed ``/api/tags``
        payload. Failures are per-worker and non-fatal: a worker whose
        inventory cannot be read keeps whatever record it already had, so a
        transient probe failure cannot erase known affinity. Returns a map of
        worker id to recorded model count, or to an error string.
        """
        with self._lock:
            endpoints = [state.endpoint for state in self._states]
        results = {}
        for endpoint in endpoints:
            try:
                payload = fetch_tags(endpoint.origin)
                rows = model_inventory.inventory_rows(payload, "/api/tags")
                names = [
                    name
                    for name in (
                        row.get("name") or row.get("model") for row in rows
                    )
                    if name
                ]
            except Exception as error:
                results[endpoint.worker_id] = "error: %s" % str(error)[:120]
                continue
            self.note_models(endpoint.origin, names)
            results[endpoint.worker_id] = len(names)
        return results

    def request(
        self,
        sender: Callable[[str], object],
        *,
        model: str | None = None,
        idempotent: bool = False,
    ):
        """Route one request without replaying ambiguous non-idempotent work.

        ``urllib`` transport exceptions do not prove that a request body was
        never delivered.  Failover is therefore reserved for explicitly
        idempotent control-plane reads.  Model POSTs select one worker exactly
        once; a timeout or connection loss after send is surfaced as uncertain
        instead of duplicating inference on another host.
        """
        last_error = None
        for state in self._ordered(model):
            self._start(state)
            started = self._time()
            try:
                result = sender(state.endpoint.origin)
            except Exception as error:
                retryable = self._retryable(error)
                self._finish(
                    state,
                    error,
                    self._time() - started,
                    count_failure=retryable,
                )
                if not idempotent or not retryable:
                    raise
                last_error = error
                continue
            self._finish(state, None, self._time() - started)
            return result

    def drain(self, *, timeout_seconds: float = 5.0) -> bool:
        """Stop admission and wait a bounded interval for in-flight calls."""
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            self._draining = True
            self._condition.notify_all()
            while any(state.inflight for state in self._states):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def snapshots(self) -> tuple[WorkerSnapshot, ...]:
        now = self._time()
        with self._lock:
            return tuple(
                WorkerSnapshot(
                    worker_id=state.endpoint.worker_id,
                    origin=state.endpoint.origin,
                    state=label,
                    healthy=healthy,
                    inflight=state.inflight,
                    capacity=capacity,
                    consecutive_failures=state.consecutive_failures,
                    last_error=state.last_error,
                    cooldown_until=state.cooldown_until,
                    trips=state.trips,
                    probing=bool(state.trips) and state.cooldown_until <= now,
                    ewma_latency_ms=round(state.ewma_latency * 1000.0, 3),
                )
                for state in self._states
            )

    def status(self) -> dict:
        workers = self.snapshots()
        with self._condition:
            metrics = dict(self._metrics)
            waiters = self._waiters
            draining = self._draining
        return {
            "enabled": self.enabled,
            "admission": "draining" if draining else "accepting",
            "worker_count": len(workers),
            "remote_worker_count": sum(
                1 for state in self._states
                if not _is_loopback(state.endpoint.origin)
            ),
            "remote_tls_required": self.has_remote_workers,
            "tls_verification": (
                "system-trust-store" if self.has_remote_workers else "not-applicable"
            ),
            "non_idempotent_failover": False,
            "workers": [snapshot.to_dict() for snapshot in self.snapshots()],
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
    env = os.environ if environment is None else environment
    with _configuration_lock:
        configured_workers = _configured_workers
        configured_allow_remote = _configured_allow_remote
    if (
        environment is None
        and configured_workers is not None
        and configured_allow_remote is not None
    ):
        worker_origins = configured_workers
        allow_remote = configured_allow_remote
    else:
        worker_origins = parse_worker_origins(env.get("SONDER_OLLAMA_WORKERS"))
        allow_remote = str(env.get("SONDER_ALLOW_REMOTE_OLLAMA", "")).strip().lower() in {
            "1", "true", "yes", "on",
        }
    return OllamaWorkerPool(
        primary_origin,
        worker_origins,
        allow_remote=allow_remote,
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
    "parse_worker_origins",
    "reset_typed_workers",
    "validate_worker_origin",
]
