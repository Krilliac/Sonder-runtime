from __future__ import annotations

from dataclasses import dataclass

from sonder_runtime.application.jobs.durable_registry import (
    ProcessTreeCleanupReceipt,
)
from sonder_runtime.application.operations.graceful_drain import (
    DrainStage,
    GracefulDrainCoordinator,
    GracefulDrainRequest,
)
from sonder_runtime.application.operations.startup_reconciliation import (
    RecordKind,
    StartupObservation,
)


@dataclass
class Admission:
    calls: list[str]

    def stop_admission(self, reason: str) -> bool:
        self.calls.append(reason)
        return True


@dataclass
class Deadline:
    notices: list

    def announce_deadline(self, notice) -> bool:
        self.notices.append(notice)
        return True


@dataclass
class Descendants:
    calls: list[str]
    settled: list[float]

    def cancel_descendants(self, reason: str) -> bool:
        self.calls.append(reason)
        return True

    def settle_descendants(self, deadline_monotonic: float) -> bool:
        self.settled.append(deadline_monotonic)
        return True


class ProcessTree:
    def __init__(self, complete: bool = True) -> None:
        self.complete = complete
        self.requests = []

    def cleanup(self, request):
        self.requests.append(request)
        return ProcessTreeCleanupReceipt(
            request.job_id, True, 2, 2 if self.complete else 1,
            self.complete, "terminated" if self.complete else "descendant remains",
        )


def _coordinator(process_tree, *, clock=lambda: 0.0):
    admission = Admission([])
    deadline = Deadline([])
    descendants = Descendants([], [])
    coordinator = GracefulDrainCoordinator(
        admission=admission,
        descendants=descendants,
        deadline_communicator=deadline,
        flush=lambda remaining: remaining >= 0,
        cleanup=lambda remaining: remaining >= 0,
        process_tree=process_tree,
        clock=clock,
    )
    return coordinator, admission, deadline, descendants


def test_graceful_drain_orders_admission_deadline_settle_flush_and_cleanup():
    process_tree = ProcessTree()
    coordinator, admission, deadline, descendants = _coordinator(process_tree)
    result = coordinator.drain(
        GracefulDrainRequest(reason="deploy", deadline_seconds=5),
        observations=(StartupObservation(
            RecordKind.JOB, "job-1", "running", owner_alive=False,
            process_id=42, process_group_id=7,
        ),),
    )

    assert result.clean
    assert result.stage is DrainStage.COMPLETE
    assert admission.calls == ["deploy"]
    assert len(deadline.notices) == 1
    assert descendants.calls == ["deploy"]
    assert len(descendants.settled) == 1
    assert len(process_tree.requests) == 1
    assert process_tree.requests[0].max_descendants == 64


def test_incomplete_process_tree_is_never_reported_as_clean():
    coordinator, *_ = _coordinator(ProcessTree(complete=False))
    result = coordinator.drain(
        GracefulDrainRequest(),
        observations=(StartupObservation(
            RecordKind.JOB, "job-1", "active", owner_alive=False, process_id=9,
        ),),
    )

    assert result.stage is DrainStage.INCOMPLETE
    assert not result.clean
    assert result.cleanup_completed is False
    assert result.process_tree[0].complete is False


def test_deadline_expiry_is_truthful_and_bounds_process_cleanup():
    ticks = iter((0.0, 0.0, 0.0, 2.0, 2.0, 2.0, 2.0))
    coordinator, *_ = _coordinator(ProcessTree(), clock=lambda: next(ticks, 2.0))
    result = coordinator.drain(GracefulDrainRequest(deadline_seconds=1))

    assert result.stage is DrainStage.INCOMPLETE
    assert result.timed_out
    assert not result.clean
    assert any("deadline" in error for error in result.errors)


def test_request_rejects_unbounded_or_invalid_values():
    for kwargs in (
        {"reason": ""},
        {"deadline_seconds": 0},
        {"max_records": 0},
        {"max_process_descendants": 0},
    ):
        try:
            GracefulDrainRequest(**kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid request was accepted")
