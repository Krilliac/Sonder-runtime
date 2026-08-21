"""Process and dependency state for the production service lifecycle.

SPEC-2 section 4 defines one process state and one dependency state.  The
process state answers "may this runtime accept work right now", the
dependency states answer "why not".  Transitions are validated so that a
bug cannot silently report READY from a process that never migrated, and
DRAINING can never be re-entered as READY without a restart.

The tracker is deliberately synchronous and lock-protected: it is consulted
from HTTP handler threads, signal handlers, and the shutdown coordinator.
"""
from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass, field


class ProcessState(enum.Enum):
    STARTING = "starting"
    MIGRATING = "migrating"
    READY = "ready"
    DEGRADED = "degraded"
    DRAINING = "draining"
    STOPPING = "stopping"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class DependencyState(enum.Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


# Allowed process transitions.  A transition not listed here raises
# InvalidTransition instead of quietly corrupting the reported state.
_ALLOWED: dict[ProcessState, frozenset[ProcessState]] = {
    ProcessState.STARTING: frozenset(
        {ProcessState.MIGRATING, ProcessState.FAILED}
    ),
    ProcessState.MIGRATING: frozenset(
        {ProcessState.READY, ProcessState.RECOVERY_REQUIRED, ProcessState.FAILED}
    ),
    ProcessState.RECOVERY_REQUIRED: frozenset(
        {ProcessState.READY, ProcessState.FAILED}
    ),
    ProcessState.READY: frozenset(
        {ProcessState.DEGRADED, ProcessState.DRAINING, ProcessState.FAILED}
    ),
    ProcessState.DEGRADED: frozenset(
        {ProcessState.READY, ProcessState.DRAINING, ProcessState.FAILED}
    ),
    ProcessState.DRAINING: frozenset({ProcessState.STOPPING}),
    ProcessState.STOPPING: frozenset(),
    ProcessState.FAILED: frozenset(),
}

TERMINAL_STATES = frozenset({ProcessState.STOPPING, ProcessState.FAILED})

# States in which the runtime reports ready and may admit application work.
SERVING_STATES = frozenset({ProcessState.READY, ProcessState.DEGRADED})


class InvalidTransition(RuntimeError):
    """Raised for a process-state transition SPEC-2 does not allow."""

    def __init__(self, current: ProcessState, requested: ProcessState) -> None:
        super().__init__(
            f"process state may not move {current.value} -> {requested.value}"
        )
        self.current = current
        self.requested = requested


@dataclass(frozen=True)
class DependencyReport:
    name: str
    state: DependencyState
    required: bool
    detail: str = ""
    checked_at_monotonic: float = 0.0


@dataclass
class _Dependency:
    state: DependencyState = DependencyState.UNKNOWN
    required: bool = True
    detail: str = ""
    checked_at_monotonic: float = 0.0


@dataclass(frozen=True)
class StateSnapshot:
    process: ProcessState
    since_monotonic: float
    reason: str
    dependencies: tuple[DependencyReport, ...] = ()

    @property
    def serving(self) -> bool:
        return self.process in SERVING_STATES

    def as_dict(self) -> dict:
        return {
            "process_state": self.process.value,
            "reason": self.reason,
            "dependencies": {
                dep.name: {
                    "state": dep.state.value,
                    "required": dep.required,
                    "detail": dep.detail,
                }
                for dep in self.dependencies
            },
        }


class ServiceStateTracker:
    """Thread-safe holder for the process state and dependency states."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = ProcessState.STARTING
        self._since = time.monotonic()
        self._reason = "process starting"
        self._dependencies: dict[str, _Dependency] = {}
        self._listeners: list = []

    # -- process state -----------------------------------------------------

    def transition(self, target: ProcessState, reason: str = "") -> StateSnapshot:
        with self._lock:
            if target is self._state:
                return self._snapshot_locked()
            if target not in _ALLOWED[self._state]:
                raise InvalidTransition(self._state, target)
            self._state = target
            self._since = time.monotonic()
            self._reason = reason or target.value
            snapshot = self._snapshot_locked()
            listeners = list(self._listeners)
        for listener in listeners:
            listener(snapshot)
        return snapshot

    def fail(self, reason: str) -> StateSnapshot:
        """Force FAILED from any non-terminal state (unrecoverable invariant)."""
        with self._lock:
            if self._state not in TERMINAL_STATES:
                self._state = ProcessState.FAILED
                self._since = time.monotonic()
                self._reason = reason
            snapshot = self._snapshot_locked()
            listeners = list(self._listeners)
        for listener in listeners:
            listener(snapshot)
        return snapshot

    def add_listener(self, callback) -> None:
        """Register a callback invoked with each new StateSnapshot."""
        with self._lock:
            self._listeners.append(callback)

    # -- dependencies ------------------------------------------------------

    def register_dependency(self, name: str, *, required: bool) -> None:
        with self._lock:
            self._dependencies.setdefault(name, _Dependency(required=required))
            self._dependencies[name].required = required

    def set_dependency(
        self, name: str, state: DependencyState, detail: str = ""
    ) -> StateSnapshot:
        """Record a dependency probe result and derive READY/DEGRADED.

        Losing an *optional* dependency degrades a READY process; recovery
        restores READY.  Required-dependency loss is reported through
        readiness (the snapshot stops being ready_for_traffic) rather than
        by faking a process-level failure, so a transient Ollama outage does
        not permanently poison the state machine.
        """
        with self._lock:
            dep = self._dependencies.setdefault(name, _Dependency())
            dep.state = state
            dep.detail = detail
            dep.checked_at_monotonic = time.monotonic()
            self._derive_degraded_locked()
            return self._snapshot_locked()

    def _derive_degraded_locked(self) -> None:
        if self._state not in SERVING_STATES:
            return
        optional_lost = any(
            not dep.required
            and dep.state in (DependencyState.DEGRADED, DependencyState.UNAVAILABLE)
            for dep in self._dependencies.values()
        )
        if optional_lost and self._state is ProcessState.READY:
            self._state = ProcessState.DEGRADED
            self._since = time.monotonic()
            self._reason = "optional dependency lost"
        elif not optional_lost and self._state is ProcessState.DEGRADED:
            self._state = ProcessState.READY
            self._since = time.monotonic()
            self._reason = "optional dependency recovered"

    # -- inspection --------------------------------------------------------

    def snapshot(self) -> StateSnapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> StateSnapshot:
        return StateSnapshot(
            process=self._state,
            since_monotonic=self._since,
            reason=self._reason,
            dependencies=tuple(
                DependencyReport(
                    name=name,
                    state=dep.state,
                    required=dep.required,
                    detail=dep.detail,
                    checked_at_monotonic=dep.checked_at_monotonic,
                )
                for name, dep in sorted(self._dependencies.items())
            ),
        )

    def ready_for_traffic(self) -> tuple[bool, str]:
        """Readiness verdict: serving state plus every required dependency."""
        snap = self.snapshot()
        if not snap.serving:
            return False, f"process state is {snap.process.value}"
        for dep in snap.dependencies:
            if dep.required and dep.state is not DependencyState.READY:
                return False, f"required dependency {dep.name} is {dep.state.value}"
        return True, "ready"
