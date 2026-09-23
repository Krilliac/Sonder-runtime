"""Canonical bounded Prometheus metrics for the Sonder runtime.

The official Prometheus client remains optional.  When it is absent every
metric call is a cheap no-op, so importing this module never makes
observability a hard runtime requirement.  Label sets are fixed at
registration; no per-request free text is accepted, keeping cardinality
bounded by construction.
"""
from __future__ import annotations

import re
import threading

try:  # optional, pinned in the production lock file when enabled
    from prometheus_client import (
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised on minimal installs
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

# Identity reservations belong to the process metric owner, never roster
# positions. Removed members retain their slots; later identities overflow.
_WORKER_LABELS = frozenset({*("w%d" % index for index in range(16)), "overflow"})
_WORKER_IDENTITY = re.compile(r"[0-9a-f]{64}")
_COMPUTE_REJECTION_REASONS = frozenset({
    "node_avoided",
    "unhealthy",
    "future_observation",
    "stale",
    "workload_not_allowed",
    "local_only",
    "remote_not_allowed",
    "missing_capability",
    "missing_any_capability",
    "workspace_unavailable",
    "model_unavailable",
    "ram_unknown",
    "insufficient_ram",
    "disk_unknown",
    "insufficient_disk",
    "vram_unknown",
    "insufficient_vram",
    "load_unknown",
    "load_too_high",
    "no_candidates",
})


class _NoopMetric:
    def labels(self, *args, **kwargs):
        return self

    def inc(self, amount: float = 1.0) -> None:
        pass

    def dec(self, amount: float = 1.0) -> None:
        pass

    def set(self, value: float) -> None:
        pass

    def observe(self, value: float) -> None:
        pass

    def info(self, values: dict) -> None:
        pass


class MetricsRegistry:
    """Owner of every Sonder metric; one instance per process."""

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled and PROMETHEUS_AVAILABLE
        self._lock = threading.Lock()
        self._ollama_worker_identities: dict[str, str] = {}
        if self.enabled:
            self._registry = CollectorRegistry()
            self.build_info = Gauge(
                "sonder_build_info", "Build identity (always 1)",
                ["version", "commit"], registry=self._registry,
            )
            self.process_state = Gauge(
                "sonder_process_state",
                "Numeric process state (see sonder_service_state)",
                registry=self._registry,
            )
            self.requests_total = Counter(
                "sonder_requests_total", "HTTP requests by route and result",
                ["route", "result"], registry=self._registry,
            )
            self.request_duration_seconds = Histogram(
                "sonder_request_duration_seconds", "HTTP request duration",
                ["route"], registry=self._registry,
            )
            self.active_requests = Gauge(
                "sonder_active_requests", "In-flight HTTP requests",
                registry=self._registry,
            )
            self.admission_capacity = Gauge(
                "sonder_admission_capacity",
                "Configured HTTP admission capacity by kind",
                ["kind"], registry=self._registry,
            )
            self.admission_queue_depth = Gauge(
                "sonder_admission_queue_depth",
                "Requests currently waiting for an execution slot",
                registry=self._registry,
            )
            self.admission_queue_high_watermark = Gauge(
                "sonder_admission_queue_high_watermark",
                "Highest observed admission queue depth since process start",
                registry=self._registry,
            )
            self.admission_queue_wait_seconds = Histogram(
                "sonder_admission_queue_wait_seconds",
                "Time from admission enqueue to slot acquisition or timeout",
                registry=self._registry,
            )
            self.admission_rejections_total = Counter(
                "sonder_admission_rejections_total",
                "Admission rejections by bounded reason code",
                ["reason"], registry=self._registry,
            )
            self.request_cache_total = Counter(
                "sonder_request_cache_total",
                "Deterministic request cache consultations by result",
                ["result"], registry=self._registry,
            )
            self.model_calls_total = Counter(
                "sonder_model_calls_total", "Model calls by tier and result",
                ["tier", "result"], registry=self._registry,
            )
            self.model_call_duration_seconds = Histogram(
                "sonder_model_call_duration_seconds",
                "Model call duration by tier", ["tier"], registry=self._registry,
            )
            self.model_backend_phase_duration_seconds = Histogram(
                "sonder_model_backend_phase_duration_seconds",
                "Backend-measured inference phase duration",
                ["backend", "phase"], registry=self._registry,
            )
            self.model_token_throughput_per_second = Histogram(
                "sonder_model_token_throughput_per_second",
                "Backend-measured token throughput",
                ["backend", "direction"],
                buckets=(0.1, 0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500,
                         1000, 2000, 5000, 10000, float("inf")),
                registry=self._registry,
            )
            self.model_prompt_tokens = Histogram(
                "sonder_model_prompt_tokens",
                "Provider-reported prompt token counts by cache state",
                ["backend", "state"],
                buckets=(0, 1, 8, 32, 128, 512, 2048, 8192, 32768, 131072,
                         1_000_000, float("inf")),
                registry=self._registry,
            )
            self.model_load_states_total = Counter(
                "sonder_model_load_states_total",
                "Explicit backend load-state observations",
                ["backend", "state"], registry=self._registry,
            )
            self.sqlite_lock_wait_seconds = Histogram(
                "sonder_sqlite_lock_wait_seconds", "SQLite lock waits by store",
                ["store"], registry=self._registry,
            )
            self.task_states = Gauge(
                "sonder_task_states", "Durable task counts by kind and state",
                ["kind", "state"], registry=self._registry,
            )
            self.autopilot_runs_total = Counter(
                "sonder_autopilot_runs_total", "Autopilot runs by result",
                ["result"], registry=self._registry,
            )
            self.backup_age_seconds = Gauge(
                "sonder_backup_age_seconds", "Age of the newest verified backup",
                registry=self._registry,
            )
            self.backup_runs_total = Counter(
                "sonder_backup_runs_total", "Backup runs by result",
                ["result"], registry=self._registry,
            )
            self.disk_free_bytes = Gauge(
                "sonder_disk_free_bytes", "Free disk by path class",
                ["path_class"], registry=self._registry,
            )
            self.redaction_failures_total = Counter(
                "sonder_redaction_failures_total", "Redaction filter failures",
                registry=self._registry,
            )
            self.auth_failures_total = Counter(
                "sonder_auth_failures_total",
                "Authentication failures by reason", ["reason"],
                registry=self._registry,
            )
            self.ollama_worker_requests_total = Counter(
                "sonder_ollama_worker_requests_total",
                "Multi-PC Ollama pool requests by bounded worker slot and result",
                ["worker", "result"], registry=self._registry,
            )
            self.ollama_worker_duration_seconds = Histogram(
                "sonder_ollama_worker_duration_seconds",
                "Multi-PC Ollama pool request duration by bounded worker slot",
                ["worker"], registry=self._registry,
            )
            self.ollama_worker_circuit_state_total = Counter(
                "sonder_ollama_worker_circuit_state_total",
                "Ollama worker circuit breaker transitions by bounded worker "
                "slot and state",
                ["worker", "state"], registry=self._registry,
            )
            self.compute_nodes = Gauge(
                "sonder_compute_nodes",
                "Configured compute nodes by bounded current state",
                ["state"], registry=self._registry,
            )
            self.compute_active_jobs = Gauge(
                "sonder_compute_active_jobs",
                "Active jobs reported by the compute fabric",
                registry=self._registry,
            )
            self.compute_placements_total = Counter(
                "sonder_compute_placements_total",
                "Successful compute placements by local or remote route",
                ["route"], registry=self._registry,
            )
            self.compute_placement_rejections_total = Counter(
                "sonder_compute_placement_rejections_total",
                "Compute placement rejections by bounded reason code",
                ["reason"], registry=self._registry,
            )
        else:
            noop = _NoopMetric()
            for name in (
                "build_info", "process_state", "requests_total",
                "request_duration_seconds", "active_requests", "request_cache_total",
                "admission_capacity", "admission_queue_depth",
                "admission_queue_high_watermark", "admission_queue_wait_seconds",
                "admission_rejections_total",
                "model_calls_total", "model_call_duration_seconds",
                "model_backend_phase_duration_seconds",
                "model_token_throughput_per_second", "model_load_states_total",
                "model_prompt_tokens",
                "sqlite_lock_wait_seconds", "task_states", "autopilot_runs_total",
                "backup_age_seconds", "backup_runs_total", "disk_free_bytes",
                "redaction_failures_total", "auth_failures_total",
                "ollama_worker_requests_total", "ollama_worker_duration_seconds",
                "ollama_worker_circuit_state_total",
                "compute_nodes", "compute_active_jobs",
                "compute_placements_total", "compute_placement_rejections_total",
            ):
                setattr(self, name, noop)

    def observe_inference(self, backend: str, telemetry) -> None:
        """Record bounded, content-free measurements from a backend."""
        if telemetry is None:
            return
        backend = backend if backend in ("ollama", "openai_compatible") else "other"
        phases = (
            ("total", getattr(telemetry, "backend_total_ms", None)),
            ("load", getattr(telemetry, "load_ms", None)),
            ("prompt_eval", getattr(telemetry, "prompt_eval_ms", None)),
            ("eval", getattr(telemetry, "eval_ms", None)),
        )
        for phase, milliseconds in phases:
            if isinstance(milliseconds, (int, float)) and 0 <= milliseconds <= 86_400_000:
                self.model_backend_phase_duration_seconds.labels(
                    backend=backend, phase=phase
                ).observe(milliseconds / 1000.0)
        rates = (
            ("prompt", getattr(telemetry, "prompt_tokens_per_second", None)),
            ("output", getattr(telemetry, "output_tokens_per_second", None)),
        )
        for direction, rate in rates:
            if isinstance(rate, (int, float)) and 0 <= rate <= 1_000_000:
                self.model_token_throughput_per_second.labels(
                    backend=backend, direction=direction
                ).observe(rate)
        prompt_counts = (
            ("total", getattr(telemetry, "prompt_tokens", None)),
            ("cached", getattr(telemetry, "prompt_cached_tokens", None)),
            ("uncached", getattr(telemetry, "prompt_uncached_tokens", None)),
        )
        for state, count in prompt_counts:
            if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 1_000_000_000:
                self.model_prompt_tokens.labels(backend=backend, state=state).observe(count)
        state = getattr(telemetry, "load_state", None)
        if state in ("cold", "warm"):
            self.model_load_states_total.labels(backend=backend, state=state).inc()

    def observe_model_call(
        self, *, cloud: bool, result: str, elapsed_seconds: float
    ) -> None:
        """Record one routed generation with fixed, non-identifying labels."""
        route = "cloud" if cloud else "local"
        outcome = result if result in {"ok", "error"} else "error"
        try:
            elapsed = float(elapsed_seconds)
        except (TypeError, ValueError):
            elapsed = 0.0
        if elapsed < 0 or elapsed > 86_400:
            elapsed = 0.0
        self.model_calls_total.labels(tier=route, result=outcome).inc()
        self.model_call_duration_seconds.labels(tier=route).observe(elapsed)

    def observe_request_cache(self, result: str) -> None:
        """Record a deterministic-cache consultation with a closed label set."""
        outcome = result if result in {"hit", "miss"} else "other"
        self.request_cache_total.labels(result=outcome).inc()

    def reserve_ollama_worker_label(self, identity: str) -> str:
        """Reserve one of 16 lifetime slots by opaque identity, with no reuse."""
        if type(identity) is not str or _WORKER_IDENTITY.fullmatch(identity) is None:
            raise ValueError("worker metric identity must be a SHA-256 digest")
        with self._lock:
            existing = self._ollama_worker_identities.get(identity)
            if existing is not None:
                return existing
            if len(self._ollama_worker_identities) >= 16:
                return "overflow"
            label = "w%d" % len(self._ollama_worker_identities)
            self._ollama_worker_identities[identity] = label
            return label

    def _ollama_worker_label(self, worker: str) -> str:
        if type(worker) is str and worker in _WORKER_LABELS:
            return worker
        return "unknown"

    def observe_ollama_worker_request(
        self, *, worker: str, result: str, elapsed_seconds: float
    ) -> None:
        """Record one multi-PC pool attempt with a closed, bounded label set."""
        label = self._ollama_worker_label(worker)
        outcome = result if result in {"ok", "error"} else "error"
        try:
            elapsed = float(elapsed_seconds)
        except (TypeError, ValueError):
            elapsed = 0.0
        if elapsed < 0 or elapsed > 86_400:
            elapsed = 0.0
        self.ollama_worker_requests_total.labels(worker=label, result=outcome).inc()
        self.ollama_worker_duration_seconds.labels(worker=label).observe(elapsed)

    def observe_ollama_worker_circuit(self, *, worker: str, state: str) -> None:
        """Record a circuit-breaker transition with a closed label set."""
        label = self._ollama_worker_label(worker)
        transition = state if state in {"open", "closed"} else "open"
        self.ollama_worker_circuit_state_total.labels(
            worker=label, state=transition
        ).inc()

    def set_compute_inventory(
        self,
        *,
        configured: int,
        live: int,
        healthy: int,
        unhealthy: int,
        stale: int,
        active_jobs: int,
    ) -> None:
        """Publish content-free compute inventory using a fixed state label set."""
        values = {
            "configured": configured,
            "live": live,
            "healthy": healthy,
            "unhealthy": unhealthy,
            "stale": stale,
        }
        for state, value in values.items():
            bounded = value if type(value) is int and value >= 0 else 0
            self.compute_nodes.labels(state=state).set(bounded)
        bounded_jobs = active_jobs if type(active_jobs) is int and active_jobs >= 0 else 0
        self.compute_active_jobs.set(bounded_jobs)

    def observe_compute_placement(self, *, route: str) -> None:
        bounded = route if route in {"local", "remote"} else "other"
        self.compute_placements_total.labels(route=bounded).inc()

    def observe_compute_placement_rejection(self, *, reason: str) -> None:
        bounded = reason if reason in _COMPUTE_REJECTION_REASONS else "other"
        self.compute_placement_rejections_total.labels(reason=bounded).inc()

    def set_build_info(self, version: str, commit: str) -> None:
        self.build_info.labels(version=version, commit=commit).set(1)

    def render(self) -> bytes:
        """Return a Prometheus exposition payload or explanatory comment."""
        if not self.enabled:
            return b"# metrics disabled or prometheus_client not installed\n"
        return generate_latest(self._registry)


_default: MetricsRegistry | None = None
_default_lock = threading.Lock()


def default_registry() -> MetricsRegistry:
    global _default
    with _default_lock:
        if _default is None:
            _default = MetricsRegistry()
        return _default
