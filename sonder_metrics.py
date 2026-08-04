"""Bounded Prometheus metrics with a stdlib no-op fallback.

SPEC-2 section 10: the official Prometheus client is an *optional*
dependency.  When it is absent every metric call is a cheap no-op, so no
import of this module can make observability a hard runtime requirement.
Label sets are fixed at registration; nothing here accepts per-request
free text, which keeps cardinality bounded by construction.
"""
from __future__ import annotations

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
        if self.enabled:
            self._registry = CollectorRegistry()
            self.build_info = Gauge(
                "sonder_build_info",
                "Build identity (always 1)",
                ["version", "commit"],
                registry=self._registry,
            )
            self.process_state = Gauge(
                "sonder_process_state",
                "Numeric process state (see sonder_service_state)",
                registry=self._registry,
            )
            self.requests_total = Counter(
                "sonder_requests_total",
                "HTTP requests by route and result",
                ["route", "result"],
                registry=self._registry,
            )
            self.request_duration_seconds = Histogram(
                "sonder_request_duration_seconds",
                "HTTP request duration",
                ["route"],
                registry=self._registry,
            )
            self.active_requests = Gauge(
                "sonder_active_requests",
                "In-flight HTTP requests",
                registry=self._registry,
            )
            self.model_calls_total = Counter(
                "sonder_model_calls_total",
                "Model calls by tier and result",
                ["tier", "result"],
                registry=self._registry,
            )
            self.model_call_duration_seconds = Histogram(
                "sonder_model_call_duration_seconds",
                "Model call duration by tier",
                ["tier"],
                registry=self._registry,
            )
            self.sqlite_lock_wait_seconds = Histogram(
                "sonder_sqlite_lock_wait_seconds",
                "SQLite lock waits by store",
                ["store"],
                registry=self._registry,
            )
            self.task_states = Gauge(
                "sonder_task_states",
                "Durable task counts by kind and state",
                ["kind", "state"],
                registry=self._registry,
            )
            self.autopilot_runs_total = Counter(
                "sonder_autopilot_runs_total",
                "Autopilot runs by result",
                ["result"],
                registry=self._registry,
            )
            self.backup_age_seconds = Gauge(
                "sonder_backup_age_seconds",
                "Age of the newest verified backup",
                registry=self._registry,
            )
            self.backup_runs_total = Counter(
                "sonder_backup_runs_total",
                "Backup runs by result",
                ["result"],
                registry=self._registry,
            )
            self.disk_free_bytes = Gauge(
                "sonder_disk_free_bytes",
                "Free disk by path class",
                ["path_class"],
                registry=self._registry,
            )
            self.redaction_failures_total = Counter(
                "sonder_redaction_failures_total",
                "Redaction filter failures",
                registry=self._registry,
            )
            self.auth_failures_total = Counter(
                "sonder_auth_failures_total",
                "Authentication failures by reason",
                ["reason"],
                registry=self._registry,
            )
        else:
            noop = _NoopMetric()
            for name in (
                "build_info",
                "process_state",
                "requests_total",
                "request_duration_seconds",
                "active_requests",
                "model_calls_total",
                "model_call_duration_seconds",
                "sqlite_lock_wait_seconds",
                "task_states",
                "autopilot_runs_total",
                "backup_age_seconds",
                "backup_runs_total",
                "disk_free_bytes",
                "redaction_failures_total",
                "auth_failures_total",
            ):
                setattr(self, name, noop)

    def set_build_info(self, version: str, commit: str) -> None:
        self.build_info.labels(version=version, commit=commit).set(1)

    def render(self) -> bytes:
        """Prometheus exposition payload, or an explanatory comment."""
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
