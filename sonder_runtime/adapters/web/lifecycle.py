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

import os
import socket
import threading
import time
import urllib.request
import uuid
from collections import deque
from collections.abc import Callable, Iterable

from sonder_runtime.platform import version as sonder_version
from sonder_runtime.platform.config import SonderConfig
from sonder_runtime.application.lifecycle import process_state_number
from sonder_runtime.application.context import OperationContext, local_owner_context
from sonder_runtime.application.operations.admission_gate import (
    AdmissionClosed,
    RuntimeAdmissionGate,
)
from sonder_runtime.application.operations.graceful_drain import (
    DrainStage,
    GracefulDrainCoordinator,
    GracefulDrainRequest,
    GracefulDrainResult,
)
from sonder_runtime.application.operations.tracing_health import (
    BoundedTracer,
    HealthSnapshot,
    TraceRecord,
    health_snapshot,
)
from sonder_runtime.application.operations.startup_reconciliation import (
    RecordKind,
    StartupObservation,
    build_drain_plan,
)
from sonder_runtime.platform.metrics import MetricsRegistry
from sonder_runtime.platform.service_state import (
    DependencyState,
    ProcessState,
    ServiceStateTracker,
)
from sonder_runtime.platform.shutdown import ShutdownCoordinator

# Maintenance lock classes that block new application work entirely.
BLOCKING_MAINTENANCE_LOCKS = ("update", "restore", "migration")

_AUTH_BUCKET_CAPACITY = 10
_AUTH_BUCKET_REFILL_PER_SECOND = 0.5  # one new attempt every 2s after burst


class _BoundedTraceBuffer:
    """Process-local trace sink; it never exports or persists request content."""

    def __init__(self, maximum: int = 256) -> None:
        self._records = deque(maxlen=maximum)
        self._lock = threading.Lock()

    def export(self, record: TraceRecord) -> None:
        with self._lock:
            self._records.append(record)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            records = tuple(self._records)
        return {
            "retained": len(records),
            "max_records": self._records.maxlen,
            "export": "disabled",
            "records": tuple(
                {
                    "trace_id": item.trace_id,
                    "span_id": item.span_id,
                    "operation": item.operation,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "labels": dict(item.labels),
                    "redaction_applied": item.redaction_applied,
                }
                for item in records
            ),
        }


class LifecycleAdmissionBridge:
    """Adapt the existing shutdown admission state to OPS-005."""

    def __init__(self, coordinator: ShutdownCoordinator, tracker: ServiceStateTracker,
                 admission: RuntimeAdmissionGate) -> None:
        self._coordinator = coordinator
        self._tracker = tracker
        self._admission = admission

    def stop_admission(self, reason: str) -> bool:
        if not isinstance(reason, str) or not reason.strip():
            return False
        self._admission.stop_admission(reason)
        with self._coordinator._lock:
            if self._coordinator._draining.is_set():
                return True
            self._coordinator._draining.set()
        self._coordinator.cancellation.cancel()
        try:
            self._tracker.transition(ProcessState.DRAINING, reason)
        except Exception:
            return False
        return True


class LifecycleDescendantBridge:
    """Cancel cooperators and settle the lifecycle's counted mutations."""

    def __init__(self, coordinator: ShutdownCoordinator) -> None:
        self._coordinator = coordinator

    def cancel_descendants(self, reason: str) -> bool:
        del reason
        self._coordinator.cancellation.cancel()
        return self._coordinator.cancellation.cancelled

    def settle_descendants(self, deadline_monotonic: float) -> bool:
        with self._coordinator._idle:
            while self._coordinator._active_mutations > 0:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    return False
                self._coordinator._idle.wait(min(remaining, 0.5))
            return True


class LifecycleDeadlineBridge:
    """Record the deadline notice through the lifecycle audit surface."""

    def __init__(self, lifecycle: "RuntimeLifecycle") -> None:
        self._lifecycle = lifecycle
        self.last_notice = None

    def announce_deadline(self, notice) -> bool:
        self.last_notice = notice
        self._lifecycle.record_event(
            component="lifecycle",
            event_code="DRAIN_DEADLINE_ANNOUNCED",
            summary=notice.reason,
            detail={
                "deadline_monotonic": notice.deadline_monotonic,
                "timeout_seconds": notice.timeout_seconds,
            },
        )
        return True


class LifecycleFlushBridge:
    """Run the flush hooks registered on the existing shutdown coordinator."""

    def __init__(self, coordinator: ShutdownCoordinator) -> None:
        self._coordinator = coordinator

    def __call__(self, remaining_seconds: float) -> bool:
        del remaining_seconds
        for hook in tuple(self._coordinator._flush_hooks):
            try:
                hook()
            except Exception:
                return False
        return True


class LifecycleCleanupBridge:
    """Prove the lifecycle has no counted mutation left to clean up."""

    def __init__(self, coordinator: ShutdownCoordinator) -> None:
        self._coordinator = coordinator

    def __call__(self, remaining_seconds: float) -> bool:
        del remaining_seconds
        return (
            self._coordinator.cancellation.cancelled
            and self._coordinator.active_mutations == 0
        )


class DurableJobObservationSource:
    """Bounded durable job observations with unknown owner liveness."""

    def __init__(self, registry_factory: Callable[[], object], *, max_records: int = 101) -> None:
        if max_records < 1:
            raise ValueError("max_records must be positive")
        self._registry_factory = registry_factory
        self._max_records = max_records

    def __call__(self) -> tuple[StartupObservation, ...]:
        registry = self._registry_factory()
        records = registry.all(limit=self._max_records)
        observations = []
        for record in records:
            view = registry.view(record.identity.job_id)
            observations.append(StartupObservation(
                RecordKind.JOB,
                record.identity.job_id,
                record.status.value,
                # The registry view intentionally does not expose owner
                # liveness. Unknown is safer than claiming a process orphan.
                owner_alive=None,
                checkpoint_available=record.status.value in {"paused", "interrupted"},
                retryable=record.status.value in {"pending", "paused", "interrupted"},
                process_id=view.process_id,
                process_group_id=view.process_group_id,
            ))
        return tuple(observations)


class ProductionGracefulDrainBridge:
    """Production OPS-005 component bundle backed by existing lifecycle state."""

    def __init__(
        self,
        lifecycle: "RuntimeLifecycle",
        observation_source: DurableJobObservationSource,
        process_tree,
    ) -> None:
        self.observations = observation_source
        self.coordinator = GracefulDrainCoordinator(
            admission=LifecycleAdmissionBridge(
                lifecycle.coordinator, lifecycle.tracker, lifecycle.admission,
            ),
            descendants=LifecycleDescendantBridge(lifecycle.coordinator),
            deadline_communicator=LifecycleDeadlineBridge(lifecycle),
            flush=LifecycleFlushBridge(lifecycle.coordinator),
            cleanup=LifecycleCleanupBridge(lifecycle.coordinator),
            process_tree=process_tree,
        )


def _build_production_graceful_drain_bridge(lifecycle: "RuntimeLifecycle"):
    """Build the production bridge only when all adapter imports are present."""
    try:
        from sonder_runtime.adapters.persistence.sqlite.job_registry import (
            SQLiteDurableJobRegistry,
        )
        from sonder_runtime.adapters.process_termination import ProcessTreeSupervisor
        from sonder_runtime.platform.paths import state_path
    except Exception:
        return None

    def registry_factory():
        return SQLiteDurableJobRegistry(state_path("jobs.db", "SONDER_JOBS_DB"))

    source = DurableJobObservationSource(registry_factory)
    return ProductionGracefulDrainBridge(
        lifecycle,
        source,
        ProcessTreeSupervisor(),
    )


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
        "OWNER_CAPACITY_EXHAUSTED": "rate_limit_error",
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
        metrics_enabled: bool | None = None,
        admission_timeout_seconds: float = 10.0,
        drain_deadline_seconds: float = 25.0,
        owner_max_inflight: int | None = None,
        startup_reconciler: Callable[[], int] | None = None,
        graceful_drain_coordinator: GracefulDrainCoordinator | None = None,
        graceful_drain_observations: Callable[[], Iterable[StartupObservation]] | None = None,
    ) -> None:
        def _env_int(name: str, default: int) -> int:
            try:
                return max(1, int(os.environ.get(name, default)))
            except (TypeError, ValueError):
                return default

        self.tracker = ServiceStateTracker()
        self.tracker.register_dependency("ollama", required=True)
        self.admission = RuntimeAdmissionGate()
        self._trace_buffer = _BoundedTraceBuffer()
        self.tracer = BoundedTracer(self._trace_buffer)
        self.coordinator = ShutdownCoordinator(
            self.tracker, drain_deadline_seconds=drain_deadline_seconds
        )
        self.metrics = MetricsRegistry(
            enabled=(
                metrics_enabled
                if metrics_enabled is not None
                else os.environ.get("SONDER_METRICS", "1").strip().lower()
                in ("1", "true", "yes", "on")
            )
        )
        build = sonder_version.build_info()
        self.metrics.set_build_info(build.version, build.commit_sha)
        self._build = build

        self._max_concurrent = max_concurrent_requests or _env_int(
            "SONDER_MAX_CONCURRENT_REQUESTS", 4
        )
        self._queue_depth = queue_depth or _env_int("SONDER_QUEUE_DEPTH", 32)
        self._admission_timeout = admission_timeout_seconds
        self._drain_deadline_seconds = drain_deadline_seconds
        self._slots = threading.BoundedSemaphore(self._max_concurrent)
        self._waiters = 0
        self._admission_lock = threading.Lock()
        # One authenticated principal must never occupy every slot and queue
        # position at once; the default cap always leaves the majority of
        # total admission capacity to other owners.
        configured_owner_cap = (
            owner_max_inflight
            if owner_max_inflight is not None
            else _env_int(
                "SONDER_OWNER_MAX_INFLIGHT",
                max(1, (self._max_concurrent + self._queue_depth) // 4),
            )
        )
        # An override at or above total capacity would negate the fairness
        # bound entirely, so whenever total capacity exceeds one the
        # effective cap is clamped strictly below it: at least one admission
        # position always remains for a different owner.  With a total
        # capacity of one there is no second position to reserve and the cap
        # floors at one admission.
        total_capacity = self._max_concurrent + self._queue_depth
        if total_capacity > 1:
            self._owner_max_inflight = max(
                1, min(configured_owner_cap, total_capacity - 1)
            )
        else:
            self._owner_max_inflight = 1
        self._owner_inflight: dict[str, int] = {}

        self._auth_buckets: dict[str, _TokenBucket] = {}
        self._auth_lock = threading.Lock()

        self._ops_store = None
        self._ops_lock = threading.Lock()
        self._ops_failed = False

        self._maintenance_cache: tuple[float, tuple[str, ...]] = (0.0, ())

        self._idempotency: dict[str, dict] = {}
        self._idempotency_inflight: dict[str, threading.Event] = {}
        self._idempotency_lock = threading.Lock()

        self._probe_thread: threading.Thread | None = None
        self._probe_stop = threading.Event()
        self._startup_reconciler = startup_reconciler
        self._startup_reconciled = 0
        self._graceful_drain_coordinator = graceful_drain_coordinator
        self._graceful_drain_observations = graceful_drain_observations
        self._drain_lock = threading.Lock()
        if (
            self._graceful_drain_coordinator is None
            and self._graceful_drain_observations is None
        ):
            bridge = _build_production_graceful_drain_bridge(self)
            if bridge is not None:
                self._graceful_drain_coordinator = bridge.coordinator
                self._graceful_drain_observations = bridge.observations

    # -- operations store (lazy, never fatal) ------------------------------

    def operations(self):
        if self._ops_failed:
            return None
        with self._ops_lock:
            if self._ops_store is None:
                try:
                    import sonder_runtime.adapters.persistence.migrations as sonder_migrations
                    from sonder_runtime.adapters.persistence.operations_store import OperationsStore

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

    def operation_context(
        self, correlation_id: str, auth_context: dict | None = None,
        *, timeout_seconds: float = 30.0,
    ) -> OperationContext:
        """Create the typed context carried by the live HTTP boundary."""
        auth = auth_context or {}
        account = auth.get("account") or {}
        role = account.get("role") if isinstance(account, dict) else None
        auth_level = role if role in {"user", "developer", "admin"} else "local"
        principal = account.get("username") if isinstance(account, dict) else None
        context = local_owner_context(
            correlation_id=correlation_id,
            source="http",
            auth_level=auth_level,
            timeout_seconds=timeout_seconds,
            cancellation=self.coordinator.cancellation,
        )
        if principal:
            return OperationContext(
                correlation_id=context.correlation_id,
                principal_id=str(principal),
                auth_level=context.auth_level,
                source=context.source,
                deadline_monotonic=context.deadline_monotonic,
                cancellation=context.cancellation,
            )
        return context

    def trace_operation(
        self, context: OperationContext, *, operation: str, status: str,
        duration_ms: float, labels: dict[str, object] | None = None,
    ) -> TraceRecord:
        return self.tracer.emit(
            context,
            operation=operation,
            status=status,
            duration_ms=duration_ms,
            labels=labels,
        )

    def telemetry_snapshot(self) -> dict[str, object]:
        return self._trace_buffer.snapshot()

    # -- startup / shutdown ------------------------------------------------

    @staticmethod
    def _reconcile_startup_records() -> int:
        from sonder_runtime.adapters.persistence.sqlite.job_registry import (
            SQLiteDurableJobRegistry,
        )
        from sonder_runtime.adapters.persistence.autopilot_repository import (
            AutopilotRepository,
        )
        from sonder_runtime.adapters.persistence import fleet_store
        from sonder_runtime.platform.paths import state_path

        now_epoch = time.time()
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch))
        registry = SQLiteDurableJobRegistry(
            state_path("jobs.db", "SONDER_JOBS_DB")
        )
        jobs = registry.reconcile(now=now_iso)
        autopilot = AutopilotRepository().reconcile_stale_runs(now_epoch)
        fleet = fleet_store.reconcile_stale_owners(now=now_epoch)
        return jobs + autopilot + int(fleet.get("interrupted", 0))

    def reconcile_startup(self) -> int:
        """Reconcile durable work before the process can publish READY."""
        reconciler = self._startup_reconciler or self._reconcile_startup_records
        return int(reconciler())

    def startup(self, *, run_migrations: bool = True) -> None:
        """STARTING -> MIGRATING -> READY, used by the serve entry point."""
        snapshot = self.tracker.snapshot()
        if snapshot.process is not ProcessState.STARTING:
            return
        self.tracker.transition(ProcessState.MIGRATING, "applying migrations")
        if run_migrations:
            import sonder_runtime.adapters.persistence.migrations as sonder_migrations

            sonder_migrations.migrate_all()
        try:
            self._startup_reconciled = self.reconcile_startup()
        except Exception as exc:
            self.tracker.transition(
                ProcessState.RECOVERY_REQUIRED,
                f"startup reconciliation failed: {type(exc).__name__}",
            )
            self.metrics.process_state.set(_state_number(self.tracker))
            raise
        self.tracker.transition(ProcessState.READY, "startup complete")
        self.metrics.process_state.set(_state_number(self.tracker))
        self.tracker.add_listener(
            lambda snap: self.metrics.process_state.set(_state_number(self.tracker))
        )
        self.coordinator.install_signal_handlers(self.drain)
        self.record_event(
            component="lifecycle",
            event_code="PROCESS_READY",
            summary="runtime ready",
            detail={
                "version": self._build.version,
                "jobs_reconciled": self._startup_reconciled,
            },
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
        if not self._drain_lock.acquire(blocking=False):
            return False
        try:
            return self._drain_locked(reason)
        finally:
            self._drain_lock.release()

    def _drain_locked(self, reason: str) -> bool:
        self.record_event(
            component="lifecycle",
            event_code="DRAIN_REQUESTED",
            summary=reason,
        )
        sd_notify("STOPPING=1")
        if self._graceful_drain_coordinator is not None:
            result = self.drain_gracefully(reason)
            if result.clean:
                try:
                    self.tracker.transition(ProcessState.STOPPING, "drain complete")
                except Exception:
                    pass
            return result.clean
        clean = self.coordinator.drain(reason=reason)
        self.stop_probe()
        return clean

    def drain_gracefully(
        self,
        reason: str = "graceful shutdown requested",
        *,
        deadline_seconds: float | None = None,
        observations: Iterable[StartupObservation] | None = None,
    ) -> GracefulDrainResult:
        """Run the explicit OPS-005 bridge and report its barriers truthfully."""
        request = GracefulDrainRequest(
            reason=reason,
            deadline_seconds=(
                self._drain_deadline_seconds
                if deadline_seconds is None else deadline_seconds
            ),
        )
        plan = build_drain_plan(())
        coordinator = self._graceful_drain_coordinator
        if coordinator is None:
            return GracefulDrainResult(
                request=request,
                stage=DrainStage.INCOMPLETE,
                admission_stopped=False,
                deadline_announced=False,
                descendants_cancelled=False,
                descendants_settled=False,
                flush_completed=False,
                cleanup_completed=False,
                process_tree=(),
                plan=plan,
                errors=(
                    "graceful drain bridge is not configured; "
                    "legacy ShutdownCoordinator remains authoritative",
                ),
            )

        try:
            selected_observations = (
                tuple(self._graceful_drain_observations())
                if observations is None and self._graceful_drain_observations is not None
                else tuple(observations or ())
            )
            result = coordinator.drain(
                request,
                observations=selected_observations,
            )
        except Exception as exc:
            return GracefulDrainResult(
                request=request,
                stage=DrainStage.INCOMPLETE,
                admission_stopped=False,
                deadline_announced=False,
                descendants_cancelled=False,
                descendants_settled=False,
                flush_completed=False,
                cleanup_completed=False,
                process_tree=(),
                plan=plan,
                errors=(f"graceful drain bridge: {type(exc).__name__}",),
            )
        finally:
            self.stop_probe()
        return result

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

    def acquire_request_slot(self, *, mutating: bool = True, owner: str = ""):
        """Context manager running the SPEC-2 admission algorithm.

        ``owner`` is an opaque per-principal key used only for in-memory
        fairness accounting: an owner already at its in-flight cap is refused
        with an owner-scoped 429 *before* it consumes a queue position, so a
        single principal can never occupy every slot and queue slot at once.
        An empty owner (local-open deployments: one operator, no second party
        to protect) is bounded only by the global limits.  Owner keys are
        never persisted and never used as metric labels.
        """
        lifecycle = self

        class _Slot:
            def __init__(self) -> None:
                self._acquired = False
                self._counted_mutation = False
                self._counted_owner = False

            def _release_owner(self) -> None:
                if not self._counted_owner:
                    return
                self._counted_owner = False
                with lifecycle._admission_lock:
                    remaining = lifecycle._owner_inflight.get(owner, 0) - 1
                    if remaining > 0:
                        lifecycle._owner_inflight[owner] = remaining
                    else:
                        # Remove entries at zero so the map stays bounded by
                        # concurrently active owners.
                        lifecycle._owner_inflight.pop(owner, None)

            def __enter__(self):
                if mutating:
                    try:
                        lifecycle.admission.admit()
                    except AdmissionClosed as exc:
                        raise AdmissionRejected(
                            503, "DRAINING", str(exc), retryable=True,
                        ) from exc
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
                    if owner:
                        held = lifecycle._owner_inflight.get(owner, 0)
                        if held >= lifecycle._owner_max_inflight:
                            raise AdmissionRejected(
                                429,
                                "OWNER_CAPACITY_EXHAUSTED",
                                "this account has reached its concurrent "
                                "request limit",
                                retryable=True,
                            )
                    if lifecycle._waiters >= lifecycle._queue_depth:
                        raise AdmissionRejected(
                            429,
                            "CAPACITY_EXHAUSTED",
                            "the runtime is at its configured concurrency limit",
                            retryable=True,
                        )
                    lifecycle._waiters += 1
                    if owner:
                        lifecycle._owner_inflight[owner] = held + 1
                        self._counted_owner = True
                try:
                    acquired = lifecycle._slots.acquire(
                        timeout=lifecycle._admission_timeout
                    )
                except BaseException:
                    with lifecycle._admission_lock:
                        lifecycle._waiters -= 1
                    self._release_owner()
                    raise
                with lifecycle._admission_lock:
                    lifecycle._waiters -= 1
                if not acquired:
                    self._release_owner()
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
                self._release_owner()
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

    def idempotent(self, key: str, factory, *, cache_ttl_seconds=None,
                   cache_result=None) -> dict:
        """Return the recorded response for ``key`` or compute and record.

        ``cache_ttl_seconds`` lets a durable caller keep process-local replay
        results no longer than its durable receipt. ``cache_result`` can opt a
        retryable refusal out of the completed-result cache while preserving
        same-key in-flight coalescing.
        """
        if not key:
            return factory()
        while True:
            with self._idempotency_lock:
                cached = self._idempotency.get(key)
                if cached is not None:
                    result, expires_at = cached
                    if expires_at is None or time.monotonic() < expires_at:
                        return result
                    self._idempotency.pop(key, None)
                pending = self._idempotency_inflight.get(key)
                if pending is None:
                    pending = threading.Event()
                    self._idempotency_inflight[key] = pending
                    break
            # Only the first request executes the side effect for this key.
            pending.wait()
        try:
            result = factory()
        except BaseException:
            with self._idempotency_lock:
                self._idempotency_inflight.pop(key).set()
            raise
        with self._idempotency_lock:
            if len(self._idempotency) > 1024:
                self._idempotency.clear()
            should_cache = cache_result(result) if cache_result else True
            if should_cache:
                try:
                    ttl = None if cache_ttl_seconds is None else max(
                        0.0, float(cache_ttl_seconds)
                    )
                except (TypeError, ValueError):
                    ttl = None
                self._idempotency[key] = (
                    result, None if ttl is None else time.monotonic() + ttl,
                )
            self._idempotency_inflight.pop(key).set()
            return result

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
        ready, ready_reason = self.tracker.ready_for_traffic()
        typed_health: HealthSnapshot = health_snapshot(
            live=snapshot.process not in {ProcessState.FAILED, ProcessState.STOPPING},
            ready=ready,
            dependencies={dep.name: dep.state.value for dep in snapshot.dependencies},
            degraded=snapshot.process is ProcessState.DEGRADED,
            draining=self.coordinator.draining,
            recovery_required=snapshot.process is ProcessState.RECOVERY_REQUIRED,
            detail=ready_reason,
        )
        payload["typed_health"] = typed_health.as_dict()
        admission = self.admission.snapshot()
        payload["admission"] = {
            "accepting": admission.accepting,
            "stop_reason": admission.stop_reason,
            "accepted": admission.accepted,
            "rejected": admission.rejected,
        }
        payload["telemetry"] = self.telemetry_snapshot()
        try:
            import sonder_runtime.adapters.persistence.migrations as sonder_migrations

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


# Compatibility name retained for lifecycle callers and tests.
_state_number = process_state_number


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
_configured_config: SonderConfig | None = None


def configure(config: SonderConfig | None) -> None:
    """Bind typed runtime settings without constructing the lazy singleton."""
    global _configured_config
    if config is not None and not isinstance(config, SonderConfig):
        raise TypeError("config must be a SonderConfig when provided")
    with _instance_lock:
        if _configured_config is config:
            return
        _configured_config = config
        if _instance is not None:
            _reset_instance()


def _reset_instance() -> None:
    global _instance
    _instance = None


def get() -> RuntimeLifecycle:
    global _instance
    with _instance_lock:
        if _instance is None:
            config = _configured_config
            if config is None:
                _instance = RuntimeLifecycle()
            else:
                _instance = RuntimeLifecycle(
                    max_concurrent_requests=config.capacity.http_requests,
                    queue_depth=config.capacity.queue_depth,
                    metrics_enabled=config.observability.metrics_enabled,
                    owner_max_inflight=(
                        config.server.owner_max_inflight
                        or max(1, (config.capacity.http_requests + config.capacity.queue_depth) // 4)
                    ),
                )
        return _instance


def reset_for_tests() -> None:
    global _configured_config
    with _instance_lock:
        _configured_config = None
        _reset_instance()
