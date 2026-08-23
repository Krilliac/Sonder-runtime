"""Bounded multi-host Ollama inference routing.

The pool is deliberately an inference transport, not a distributed model
runtime. Each worker owns its Ollama process and model files; the coordinator
selects one worker for a request and can fail over only when no response was
received. A completed request is never replayed on another host.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import urllib.error

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


def parse_worker_origins(raw: str | None) -> tuple[str, ...]:
    """Parse a bounded comma/semicolon-separated worker origin list."""
    values = []
    for item in str(raw or "").replace(";", ",").split(","):
        value = item.strip()
        if value:
            values.append(value)
    return tuple(values)


def validate_worker_origin(origin: str, *, allow_remote: bool) -> str:
    """Normalize one worker origin under the Ollama trust policy."""
    normalized = ollama_policy.normalize(origin)
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("worker endpoint must not contain inline credentials")
    if not parsed.hostname or parsed.port is None:
        raise ValueError("worker endpoint must include a host and explicit port")
    if not _is_loopback(normalized):
        if not allow_remote:
            raise ValueError(
                "remote worker endpoints require SONDER_ALLOW_REMOTE_OLLAMA=1"
            )
        if parsed.scheme.casefold() != "https":
            raise ValueError("remote worker endpoints must use https")
    return normalized.rstrip("/")


@dataclass(frozen=True)
class WorkerEndpoint:
    origin: str
    worker_id: str
    metric_label: str


@dataclass(frozen=True)
class WorkerSnapshot:
    worker_id: str
    origin: str
    healthy: bool
    inflight: int
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
            "healthy": self.healthy,
            "inflight": self.inflight,
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
    """Thread-safe least-inflight scheduler for independent Ollama hosts."""

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
        if failure_threshold < 1:
            raise ValueError("failure threshold must be >= 1")
        if cooldown_seconds < 1:
            raise ValueError("cooldown seconds must be >= 1")
        all_origins = (primary_origin, *worker_origins)
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
        return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))

    def _start(self, state: _WorkerState) -> None:
        with self._lock:
            state.inflight += 1

    def _finish(
        self,
        state: _WorkerState,
        error: BaseException | None,
        elapsed: float | None = None,
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
            state.consecutive_failures += 1
            # Truncate before redacting: an unbounded exception string should
            # never reach the (relatively expensive) regex redaction passes,
            # matching the local-observability sink's truncate-then-redact
            # order for adapter-originated free text.
            raw = "%s: %s" % (type(error).__name__, str(error)[:240])
            state.last_error = self._redactor.redact(raw)[:240]
            self._metrics.observe_ollama_worker_request(
                worker=label, result="error", elapsed_seconds=elapsed or 0.0,
            )
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

    def request(self, sender: Callable[[str], object], *, model: str | None = None):
        """Send once per selected worker, failing over only before a response.

        ``model`` is an optional scheduling hint: workers whose recorded
        inventory (see ``note_models``) lacks it are tried last. It never
        changes the payload the sender transmits.
        """
        last_error = None
        for state in self._ordered(model):
            self._start(state)
            started = self._time()
            try:
                result = sender(state.endpoint.origin)
            except Exception as error:
                self._finish(state, error, self._time() - started)
                if not self._retryable(error):
                    raise
                last_error = error
                continue
            self._finish(state, None, self._time() - started)
            return result
        if last_error is not None:
            raise last_error
        raise RuntimeError("no Ollama worker is available")

    def snapshots(self) -> tuple[WorkerSnapshot, ...]:
        now = self._time()
        with self._lock:
            return tuple(
                WorkerSnapshot(
                    worker_id=state.endpoint.worker_id,
                    origin=state.endpoint.origin,
                    healthy=state.cooldown_until <= now,
                    inflight=state.inflight,
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
        return {
            "enabled": self.enabled,
            "worker_count": len(self._states),
            "remote_worker_count": sum(
                1 for state in self._states
                if not _is_loopback(state.endpoint.origin)
            ),
            "workers": [snapshot.to_dict() for snapshot in self.snapshots()],
        }


def from_environment(primary_origin: str, environment=None) -> OllamaWorkerPool:
    """Build the pool from ``SONDER_OLLAMA_WORKERS`` and consent settings."""
    env = os.environ if environment is None else environment
    allow_remote = str(env.get("SONDER_ALLOW_REMOTE_OLLAMA", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    return OllamaWorkerPool(
        primary_origin,
        parse_worker_origins(env.get("SONDER_OLLAMA_WORKERS")),
        allow_remote=allow_remote,
    )


__all__ = [
    "OllamaWorkerPool",
    "WorkerEndpoint",
    "WorkerSnapshot",
    "from_environment",
    "parse_worker_origins",
    "validate_worker_origin",
]
