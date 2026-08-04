"""Production lifecycle and admission layer for the HTTP adapter.

SPEC-2 WP3/WP4.  The stdlib HTTP server in sonder_serve.py stays; this
module owns everything production-shaped around it:

- the process/dependency state (ServiceStateTracker) and drain
  coordination (ShutdownCoordinator),
- ``/live``, ``/ready``, ``/health``, ``/version``, ``/metrics``,
- ``POST /v1/admin/drain`` with idempotency keys,
- bounded HTTP concurrency with queue-depth rejection and admission
  deadlines,
- the authentication-failure token bucket,
- the standard error envelope with correlation IDs,
- operations-store audit events for security-sensitive actions,
- systemd readiness notification (sd_notify) where available.

Everything is lazily initialised so importing this module has no side
effects; the legacy ``python sonder_serve.py`` path keeps working and
adopts the production behavior on first use.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.request
import uuid

import sonder_version
from sonder_metrics import MetricsRegistry
from sonder_service_state import (
    DependencyState,
    ProcessState,
    ServiceStateTracker,
)
from sonder_shutdown import ShutdownCoordinator

# Maintenance lock classes that block new application work entirely.
BLOCKING_MAINTENANCE_LOCKS = ("update", "restore", "migration")

_AUTH_BUCKET_CAPACITY = 10
_AUTH_BUCKET_REFILL_PER_SECOND = 0.5  # one new attempt every 2s after burst


class AdmissionRejected(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


def error_envelope(
    code: str, message: str, correlation_id: str, *, retryable: bool
) -> dict:
    """SPEC-2 WP4 envelope, plus the legacy "type" key OpenAI clients read."""
    legacy_type = {
        "CAPACITY_EXHAUSTED": "rate_limit_error",
        "ADMISSION_TIMEOUT": "server_error",
        "MAINTENANCE_MODE": "server_error",
        "DRAINING": "server_error",
        "AUTH_RATE_LIMITED": "rate_limit_error",
        "UNAUTHENTICATED": "auth",
    }.get(code, "server_error")
    return {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
            "retryable": retryable,
            "type": legacy_type,
        }
    }


def new_correlation_id() -> str:
    return "req_" + uuid.uuid4().hex


class _TokenBucket:
    __slots__ = ("tokens", "updated")

    def __init__(self) -> None:
        self.tokens = float(_AUTH_BUCKET_CAPACITY)
        self.updated = time.monotonic()


class RuntimeLifecycle:
    """One per process.  Access through :func:`get`."""

    def __init__(
        self,
        *,
        max_concurrent_requests: int | None = None,
        queue_depth: int | None = None,
        admission_timeout_seconds: float = 10.0,
        drain_deadline_seconds: float = 25.0,
    ) -> None:
        def _env_int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, default)))
            except (TypeError, ValueError):
                return default

        self.tracker = ServiceStateTracker()
        self.tracker.register_dependency("ollama", required=True)
        self.coordinator = ShutdownCoordinator(
            self.tracker, drain_deadline_seconds=drain_deadline_seconds
        )
        self.metrics = MetricsRegistry(
            enabled=os.environ.get("SONDER_METRICS", "1").strip().lower()
            in ("1", "true", "yes", "on")
        )
        build = sonder_version.build_info()
        self.metrics.set_build_info(build.version, build.commit_sha)
        self._build = build

        self._max_concurrent = max_concurrent_requests or _env_int(
            "SONDER_MAX_CONCURRENT_REQUESTS", 4
        )
        self._queue_depth = queue_depth or _env_int("SONDER_QUEUE_DEPTH", 32)
        self._admission_timeout = admission_timeout_seconds
        self._slots = threading.BoundedSemaphore(self._max_concurrent)
        self._waiters = 0
        self._admission_lock = threading.Lock()

        self._auth_buckets: dict[str, _TokenBucket] = {}
        self._auth_lock = threading.Lock()

        self._ops_store = None
        self._ops_lock = threading.Lock()
        self._ops_failed = False

        self._maintenance_cache: tuple[float, tuple[str, ...]] = (0.0, ())

        self._idempotency: dict[str, dict] = {}
        self._idempotency_lock = threading.Lock()

        self._probe_thread: threading.Thread | None = None
        self._probe_stop = threading.Event()

    # -- operations store (lazy, never fatal) ------------------------------

    def operations(self):
        if self._ops_failed:
            return None
        with self._ops_lock:
            if self._ops_store is None:
                try:
                    import sonder_migrations
                    from sonder_operations_store import OperationsStore

                    sonder_migrations.migrate_store("operations")
                    self._ops_store = OperationsStore()
                except Exception:
                    # Audit storage being broken must not take chat down,
                    # but it is visible through /health.
                    self._ops_failed = True
                    return None
            return self._ops_store

    def record_event(self, **kwargs) -> None:
        store = self.operations()
        if store is None:
            return
        try:
            store.record_event(**kwargs)
        except Exception:
            pass

    # -- startup / shutdown ------------------------------------------------

    def startup(self, *, run_migrations: bool = True) -> None:
        """STARTING -> MIGRATING -> READY, used by the serve entry point."""
        snapshot = self.tracker.snapshot()
        if snapshot.process is not ProcessState.STARTING:
            return
        self.tracker.transition(ProcessState.MIGRATING, "applying migrations")
        if run_migrations:
            import sonder_migrations

            sonder_migrations.migrate_all()
        self.tracker.transition(ProcessState.READY, "startup complete")
        self.metrics.process_state.set(_state_number(self.tracker))
        self.tracker.add_listener(
            lambda snap: self.metrics.process_state.set(_state_number(self.tracker))
        )
        self.coordinator.install_signal_handlers()
        self.record_event(
            component="lifecycle",
            event_code="PROCESS_READY",
            summary="runtime ready",
            detail={"version": self._build.version},
        )
        sd_notify("READY=1")

    def adopt_legacy_start(self) -> None:
        """A request arrived without startup(): the legacy script path.

        The process is demonstrably serving, so record the transition
        rather than reporting STARTING forever.
        """
        snapshot = self.tracker.snapshot()
        if snapshot.process is ProcessState.STARTING:
            self.tracker.transition(ProcessState.MIGRATING, "legacy start")
            self.tracker.transition(ProcessState.READY, "legacy start adopted")

    def begin_ollama_probe(self, *, interval_seconds: float = 15.0) -> None:
        if self._probe_thread is not None:
            return

        def probe_loop() -> None:
            while not self._probe_stop.wait(interval_seconds):
                self.probe_ollama_once()

        self.probe_ollama_once()
        self._probe_thread = threading.Thread(
            target=probe_loop, daemon=True, name="sonder-ollama-probe"
        )
        self._probe_thread.start()

    def probe_ollama_once(self, timeout: float = 5.0) -> bool:
        url = _ollama_url().rstrip("/") + "/api/tags"
        try:
            with urllib.request.urlopen(
                urllib.request.Request(url, method="GET"), timeout=timeout
            ) as response:
                healthy = response.status == 200
        except Exception:
            healthy = False
        self.tracker.set_dependency(
            "ollama",
            DependencyState.READY if healthy else DependencyState.UNAVAILABLE,
            "" if healthy else "probe failed",
        )
        return healthy

    def stop_probe(self) -> None:
        self._probe_stop.set()

    def drain(self, reason: str = "drain requested") -> bool:
        self.record_event(
            component="lifecycle",
            event_code="DRAIN_REQUESTED",
            summary=reason,
        )
        sd_notify("STOPPING=1")
        clean = self.coordinator.drain(reason=reason)
        self.stop_probe()
        return clean

    # -- admission ---------------------------------------------------------

    def blocking_maintenance(self) -> tuple[str, ...]:
        now = time.monotonic()
        cached_at, cached = self._maintenance_cache
        if now - cached_at < 2.0:
            return cached
        active: tuple[str, ...] = ()
        store = self.operations()
        if store is not None:
            try:
                active = tuple(
                    lock["lock_name"]
                    for lock in store.active_maintenance_locks()
                    if lock["lock_name"] in BLOCKING_MAINTENANCE_LOCKS
                )
            except Exception:
                active = ()
        self._maintenance_cache = (now, active)
        return active

    def acquire_request_slot(self, *, mutating: bool = True):
        """Context manager running the SPEC-2 admission algorithm."""
        lifecycle = self

        class _Slot:
            def __init__(self) -> None:
                self._acquired = False
                self._counted_mutation = False

            def __enter__(self):
                if mutating:
                    blocked = lifecycle.blocking_maintenance()
                    if blocked:
                        raise AdmissionRejected(
                            503,
                            "MAINTENANCE_MODE",
                            "maintenance in progress: " + ", ".join(blocked),
                            retryable=True,
                        )
                    if lifecycle.coordinator.draining:
                        raise AdmissionRejected(
                            503,
                            "DRAINING",
                            "the runtime is draining for shutdown",
                            retryable=True,
                        )
                with lifecycle._admission_lock:
                    if lifecycle._waiters >= lifecycle._queue_depth:
                        raise AdmissionRejected(
                            429,
                            "CAPACITY_EXHAUSTED",
                            "the runtime is at its configured concurrency limit",
                            retryable=True,
                        )
                    lifecycle._waiters += 1
                try:
                    acquired = lifecycle._slots.acquire(
                        timeout=lifecycle._admission_timeout
                    )
                finally:
                    with lifecycle._admission_lock:
                        lifecycle._waiters -= 1
                if not acquired:
                    raise AdmissionRejected(
                        504,
                        "ADMISSION_TIMEOUT",
                        "no execution slot became available in time",
                        retryable=True,
                    )
                self._acquired = True
                lifecycle.metrics.active_requests.inc()
                if mutating and lifecycle.coordinator.begin_mutation():
                    self._counted_mutation = True
                return self

            def __exit__(self, exc_type, exc, tb):
                if self._counted_mutation:
                    lifecycle.coordinator.end_mutation()
                if self._acquired:
                    lifecycle.metrics.active_requests.dec()
                    lifecycle._slots.release()
                return False

        return _Slot()

    # -- authentication failure limiting -----------------------------------

    def auth_attempt_allowed(self, peer: str) -> bool:
        with self._auth_lock:
            bucket = self._auth_buckets.get(peer)
            if bucket is None:
                return True
            now = time.monotonic()
            bucket.tokens = min(
                _AUTH_BUCKET_CAPACITY,
                bucket.tokens
                + (now - bucket.updated) * _AUTH_BUCKET_REFILL_PER_SECOND,
            )
            bucket.updated = now
            return bucket.tokens >= 1.0

    def record_auth_failure(self, peer: str, reason: str) -> None:
        with self._auth_lock:
            bucket = self._auth_buckets.setdefault(peer, _TokenBucket())
            now = time.monotonic()
            bucket.tokens = min(
                _AUTH_BUCKET_CAPACITY,
                bucket.tokens
                + (now - bucket.updated) * _AUTH_BUCKET_REFILL_PER_SECOND,
            )
            bucket.updated = now
            bucket.tokens = max(0.0, bucket.tokens - 1.0)
            if len(self._auth_buckets) > 4096:
                # Bound memory under address-spraying.
                for key in list(self._auth_buckets)[:2048]:
                    del self._auth_buckets[key]
        self.metrics.auth_failures_total.labels(reason=reason).inc()
        self.record_event(
            component="http",
            event_code="AUTH_FAILED",
            severity="WARNING",
            summary=f"authentication failed ({reason})",
            detail={"reason": reason, "peer_class": _peer_class(peer)},
        )

    # -- idempotency -------------------------------------------------------

    def idempotent(self, key: str, factory) -> dict:
        """Return the recorded response for ``key`` or compute and record."""
        if not key:
            return factory()
        with self._idempotency_lock:
            if key in self._idempotency:
                return self._idempotency[key]
        result = factory()
        with self._idempotency_lock:
            if len(self._idempotency) > 1024:
                self._idempotency.clear()
            self._idempotency.setdefault(key, result)
            return self._idempotency[key]

    # -- endpoint payloads -------------------------------------------------

    def live_payload(self) -> dict:
        return {"status": "alive"}

    def ready_payload(self) -> tuple[int, dict]:
        self.adopt_legacy_start()
        ready, reason = self.tracker.ready_for_traffic()
        return (200 if ready else 503), {
            "ready": ready,
            "reason": reason,
            "state": self.tracker.snapshot().process.value,
        }

    def health_payload(self) -> dict:
        self.adopt_legacy_start()
        snapshot = self.tracker.snapshot()
        payload = snapshot.as_dict()
        payload["build"] = self._build.as_dict()
        payload["draining"] = self.coordinator.draining
        payload["active_mutations"] = self.coordinator.active_mutations
        payload["operations_store"] = (
            "unavailable" if self._ops_failed else "ok"
        )
        try:
            import sonder_migrations

            payload["schemas"] = {
                store: {
                    "applied": len(status.applied),
                    "pending": len(status.pending),
                    "healthy": status.healthy,
                }
                for store, status in sonder_migrations.status_all().items()
            }
        except Exception as exc:
            payload["schemas"] = {"error": type(exc).__name__}
        return payload

    def version_payload(self) -> dict:
        return self._build.as_dict()


def _state_number(tracker: ServiceStateTracker) -> int:
    order = {
        ProcessState.STARTING: 0,
        ProcessState.MIGRATING: 1,
        ProcessState.READY: 2,
        ProcessState.DEGRADED: 3,
        ProcessState.DRAINING: 4,
        ProcessState.STOPPING: 5,
        ProcessState.FAILED: 6,
    }
    return order[tracker.snapshot().process]


def _peer_class(peer: str) -> str:
    import ipaddress

    try:
        return "loopback" if ipaddress.ip_address(peer).is_loopback else "remote"
    except ValueError:
        return "unknown"


def _ollama_url() -> str:
    raw = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").strip()
    return raw if "://" in raw else f"http://{raw}"


def sd_notify(message: str) -> None:
    """Best-effort systemd readiness notification (Type=notify support)."""
    path = os.environ.get("NOTIFY_SOCKET", "")
    if not path:
        return
    try:
        address = "\0" + path[1:] if path.startswith("@") else path
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(address)
            sock.send(message.encode("utf-8"))
        finally:
            sock.close()
    except OSError:
        pass


_instance: RuntimeLifecycle | None = None
_instance_lock = threading.Lock()


def get() -> RuntimeLifecycle:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = RuntimeLifecycle()
        return _instance


def reset_for_tests() -> None:
    global _instance
    with _instance_lock:
        _instance = None
