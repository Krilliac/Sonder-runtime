"""Bounded graceful-drain orchestration (REMAINING-OPS-005).

The coordinator is an application contract, not a process supervisor.  It
closes admission, communicates one deadline, asks cooperative descendants to
cancel and settle, flushes application state, and hands bounded process-tree
requests to an injected platform adapter.  A drain is reported clean only
when every requested barrier and every process-tree receipt proves completion.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import Callable, Iterable, Protocol

from .startup_reconciliation import (
    DrainPlan,
    ProcessTreeCleanupIntent,
    StartupObservation,
    build_drain_plan,
)
from ..jobs.durable_registry import (
    ProcessTreeCleanupContract,
    ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)


class DrainStage(StrEnum):
    ADMISSION_STOPPED = "admission_stopped"
    DEADLINE_ANNOUNCED = "deadline_announced"
    DESCENDANTS_SETTLED = "descendants_settled"
    FLUSHED = "flushed"
    CLEANED = "cleaned"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class GracefulDrainRequest:
    reason: str = "graceful shutdown requested"
    deadline_seconds: float = 25.0
    max_records: int = 100
    max_process_descendants: int = 64

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("reason is required")
        if isinstance(self.deadline_seconds, bool) or self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if isinstance(self.max_records, bool) or self.max_records <= 0:
            raise ValueError("max_records must be positive")
        if (isinstance(self.max_process_descendants, bool)
                or self.max_process_descendants <= 0):
            raise ValueError("max_process_descendants must be positive")


@dataclass(frozen=True, slots=True)
class DrainDeadlineNotice:
    reason: str
    deadline_monotonic: float
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class ProcessTreeDrainResult:
    """Truthful per-intent outcome; incomplete means cleanup remains."""

    record_id: str
    requested: bool
    complete: bool
    descendants_seen: int = 0
    descendants_terminated: int = 0
    detail: str = ""

    @classmethod
    def from_receipt(cls, receipt: ProcessTreeCleanupReceipt) -> "ProcessTreeDrainResult":
        return cls(
            receipt.job_id,
            receipt.requested,
            receipt.complete,
            receipt.descendants_seen,
            receipt.descendants_terminated,
            receipt.detail,
        )


@dataclass(frozen=True, slots=True)
class GracefulDrainResult:
    request: GracefulDrainRequest
    stage: DrainStage
    admission_stopped: bool
    deadline_announced: bool
    descendants_cancelled: bool
    descendants_settled: bool
    flush_completed: bool
    cleanup_completed: bool
    process_tree: tuple[ProcessTreeDrainResult, ...]
    plan: DrainPlan
    timed_out: bool = False
    errors: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        """Whether the result proves a complete graceful drain."""
        return (
            self.stage is DrainStage.COMPLETE
            and not self.timed_out
            and self.admission_stopped
            and self.deadline_announced
            and self.descendants_cancelled
            and self.descendants_settled
            and self.flush_completed
            and self.cleanup_completed
            and all(item.requested and item.complete for item in self.process_tree)
        )


class AdmissionStopper(Protocol):
    def stop_admission(self, reason: str) -> bool: ...


class DescendantController(Protocol):
    def cancel_descendants(self, reason: str) -> bool: ...
    def settle_descendants(self, deadline_monotonic: float) -> bool: ...


class DeadlineCommunicator(Protocol):
    def announce_deadline(self, notice: DrainDeadlineNotice) -> bool: ...


Hook = Callable[[float], bool]


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    return max(0.0, deadline - clock())


class GracefulDrainCoordinator:
    """Run one bounded, idempotent-at-the-boundary drain attempt.

    Dependencies are intentionally injected so HTTP lifecycle, job registry,
    provider cleanup, and platform process supervision remain replaceable.
    Hooks receive the remaining seconds and must return whether their barrier
    completed.  Exceptions are captured as incomplete outcomes rather than
    being presented as successful shutdown.
    """

    def __init__(
        self,
        *,
        admission: AdmissionStopper,
        descendants: DescendantController,
        deadline_communicator: DeadlineCommunicator,
        flush: Hook,
        cleanup: Hook,
        process_tree: ProcessTreeCleanupContract,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._admission = admission
        self._descendants = descendants
        self._deadline_communicator = deadline_communicator
        self._flush = flush
        self._cleanup = cleanup
        self._process_tree = process_tree
        self._clock = clock
        self._running = False

    def drain(
        self,
        request: GracefulDrainRequest,
        *,
        observations: Iterable[StartupObservation] = (),
    ) -> GracefulDrainResult:
        if self._running:
            raise RuntimeError("a graceful drain is already running")
        self._running = True
        deadline = self._clock() + request.deadline_seconds
        plan = build_drain_plan(
            observations,
            max_records=request.max_records,
            max_process_descendants=request.max_process_descendants,
        )
        errors: list[str] = []

        def call(label: str, operation: Callable[[], bool]) -> bool:
            if _remaining(deadline, self._clock) <= 0:
                errors.append(f"{label}: deadline expired")
                return False
            try:
                result = operation()
                if not isinstance(result, bool):
                    raise TypeError("operation must return bool")
                if not result:
                    errors.append(f"{label}: incomplete")
                return result
            except Exception as exc:  # boundary must remain truthful
                errors.append(f"{label}: {type(exc).__name__}")
                return False

        try:
            admission_stopped = call(
                "admission", lambda: self._admission.stop_admission(request.reason)
            )
            notice = DrainDeadlineNotice(request.reason, deadline, request.deadline_seconds)
            deadline_announced = call(
                "deadline", lambda: self._deadline_communicator.announce_deadline(notice)
            )
            descendants_cancelled = call(
                "descendant cancellation",
                lambda: self._descendants.cancel_descendants(request.reason),
            )
            descendants_settled = call(
                "descendant settle",
                lambda: self._descendants.settle_descendants(deadline),
            )
            flush_completed = call(
                "flush", lambda: self._flush(_remaining(deadline, self._clock))
            )

            process_results: list[ProcessTreeDrainResult] = []
            process_ok = True
            for intent in plan.cleanup_intents:
                if _remaining(deadline, self._clock) <= 0:
                    process_ok = False
                    errors.append(f"process tree {intent.record_id}: deadline expired")
                    break
                try:
                    receipt = self._process_tree.cleanup(_cleanup_request(intent))
                    if not isinstance(receipt, ProcessTreeCleanupReceipt):
                        raise TypeError("process cleanup must return ProcessTreeCleanupReceipt")
                    result = ProcessTreeDrainResult.from_receipt(receipt)
                    process_results.append(result)
                    if not result.requested or not result.complete:
                        process_ok = False
                except Exception as exc:
                    process_ok = False
                    errors.append(f"process tree {intent.record_id}: {type(exc).__name__}")
            cleanup_completed = call("cleanup", lambda: self._cleanup(_remaining(deadline, self._clock)))
            timed_out = _remaining(deadline, self._clock) <= 0
            if plan.truncated:
                errors.append("drain plan truncated: additional records remain unobserved")
            all_barriers = all((admission_stopped, deadline_announced,
                                descendants_cancelled, descendants_settled,
                                flush_completed, cleanup_completed, process_ok,
                                not plan.truncated))
            stage = DrainStage.COMPLETE if all_barriers and not timed_out else DrainStage.INCOMPLETE
            if stage is DrainStage.COMPLETE:
                stage = DrainStage.COMPLETE
            return GracefulDrainResult(
                request, stage, admission_stopped, deadline_announced,
                descendants_cancelled, descendants_settled, flush_completed,
                cleanup_completed and process_ok, tuple(process_results), plan,
                timed_out, tuple(errors),
            )
        finally:
            self._running = False


def _cleanup_request(intent: ProcessTreeCleanupIntent) -> ProcessTreeCleanupRequest:
    if intent.process_id is None or intent.process_id <= 0:
        raise ValueError("cleanup intent must contain a positive process id")
    return ProcessTreeCleanupRequest(
        intent.record_id,
        intent.process_id,
        intent.process_group_id,
        intent.max_descendants,
        intent.reason,
    )


__all__ = [
    "AdmissionStopper", "DeadlineCommunicator", "DescendantController",
    "DrainDeadlineNotice", "DrainStage", "GracefulDrainCoordinator",
    "GracefulDrainRequest", "GracefulDrainResult", "ProcessTreeDrainResult",
]
