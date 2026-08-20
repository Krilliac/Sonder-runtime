from __future__ import annotations

import pytest

from sonder_runtime.application.execution.world_control import OutputStream
from sonder_runtime.application.jobs.durable_registry import (
    DurableJobRegistry,
    ProcessTreeCleanupReceipt,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus


def identity(job_id: str, *, parent: str | None = None) -> JobIdentity:
    return JobIdentity(job_id, "workflow", f"op-{job_id}", f"idem-{job_id}", parent_job_id=parent)


def test_parent_linked_lifecycle_is_one_registry_surface() -> None:
    registry = DurableJobRegistry(clock=lambda: "t")
    parent = registry.start(identity("parent"))
    child = registry.start(identity("child", parent="parent"))
    assert child.identity.parent_job_id == parent.identity.job_id
    assert [record.identity.job_id for record in registry.list(parent_job_id="parent")] == ["child"]
    registry.append_output("child", OutputStream.STDOUT, "hello")
    assert registry.stream("child").events[0].data == "hello"
    with pytest.raises(KeyError):
        registry.start(identity("orphan", parent="missing"))


def test_cancel_propagates_to_descendants_and_collect_is_terminal_only() -> None:
    registry = DurableJobRegistry()
    registry.start(identity("parent"))
    registry.start(identity("child", parent="parent"))
    with pytest.raises(ValueError, match="not terminal"):
        registry.collect("parent")
    cancelled = registry.cancel("parent", reason="operator stop")
    assert [record.status for record in cancelled] == [JobStatus.CANCELLED, JobStatus.CANCELLED]
    assert registry.collect("child").error == "operator stop"


def test_restart_reconciliation_marks_unknown_active_work_and_builds_cleanup_request() -> None:
    registry = DurableJobRegistry()
    registry.start(identity("job"), process_id=42, process_group_id=42)
    registry.transition("job", JobStatus.RUNNING)
    plan = registry.reconcile(owner_instance_id="old", owner_alive=False, max_process_descendants=8)
    assert plan.results[0].action.value == "cleanup_process_tree"
    request = registry.cleanup_request(plan.cleanup_intents[0])
    assert request.process_id == 42 and request.max_descendants == 8
    # Cleanup is an explicit platform intent; reconciliation does not mutate
    # the active record until the supervisor reports its outcome.
    assert registry.poll("job").status is JobStatus.RUNNING


def test_cleanup_receipt_is_truthful_and_bounded() -> None:
    receipt = ProcessTreeCleanupReceipt("job", True, descendants_seen=2, descendants_terminated=2, complete=True)
    assert receipt.complete
    with pytest.raises(ValueError):
        ProcessTreeCleanupReceipt("job", True, descendants_seen=1, descendants_terminated=2)
