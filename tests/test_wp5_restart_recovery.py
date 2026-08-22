from dataclasses import replace

import pytest

from sonder_runtime.application.ports.jobs import JobStatus
from sonder_runtime.application.workflows import (
    RestartRecoveryService as ExportedRestartRecoveryService,
    WorkflowSnapshot as ExportedWorkflowSnapshot,
)
from sonder_runtime.application.workflows.restart_recovery import (
    RestartDisposition,
    RestartRecoveryService,
    WorkflowSnapshot,
)
from sonder_runtime.domain.common.errors import Conflict


def test_recovery_service_is_wired_through_workflows_package():
    assert ExportedRestartRecoveryService is RestartRecoveryService
    assert ExportedWorkflowSnapshot is WorkflowSnapshot


class Store:
    def __init__(self, snapshot):
        self.value = snapshot

    def get(self, execution_id):
        return self.value if self.value.execution_id == execution_id else None

    def compare_and_set(self, snapshot, *, expected_revision):
        if self.value.revision != expected_revision:
            return False
        self.value = snapshot
        return True


def service(status=JobStatus.INTERRUPTED, **kwargs):
    snapshot = WorkflowSnapshot("wf-1", "exec-1", status=status, next_step=3, state={"cursor": 9}, **kwargs)
    store = Store(snapshot)
    return RestartRecoveryService(store), store


def test_interrupted_snapshot_requires_restart_and_resume_is_idempotent():
    recovery, store = service()
    decision = recovery.detect_restart("exec-1", instance_id="worker-b")
    assert decision.disposition is RestartDisposition.RESTART_REQUIRED

    calls = []
    first = recovery.resume("exec-1", instance_id="worker-b", resume_key="step-3", run=lambda snap: calls.append(snap.next_step) or {"ok": True})
    second = recovery.resume("exec-1", instance_id="worker-b", resume_key="step-3", run=lambda _: calls.append("duplicate"))
    assert first.value == second.value == {"ok": True}
    assert second.replayed is True
    assert calls == [3]
    assert store.value.revision == 1


def test_terminal_workflow_is_not_rerun_and_finish_is_idempotent():
    recovery, _ = service(status=JobStatus.RUNNING, owner_instance_id="worker-a")
    finished = recovery.finish("exec-1", instance_id="worker-a", status=JobStatus.SUCCEEDED, result="done")
    assert recovery.detect_restart("exec-1", instance_id="worker-b").disposition is RestartDisposition.TERMINAL
    with pytest.raises(Conflict, match="terminal"):
        recovery.resume("exec-1", instance_id="worker-b", resume_key="again", run=lambda _: "bad")
    assert recovery.finish("exec-1", instance_id="worker-b", status=JobStatus.SUCCEEDED, result="different") == finished


def test_foreign_owner_and_compare_set_conflicts_are_safe():
    recovery, store = service(status=JobStatus.RUNNING, owner_instance_id="worker-a")
    assert recovery.detect_restart("exec-1", instance_id="worker-b").reason == "workflow belongs to another instance"
    store.value = replace(store.value, revision=7)

    def concurrent_write(_):
        store.value = replace(store.value, revision=8)
        return "value"

    with pytest.raises(Conflict, match="changed"):
        recovery.resume("exec-1", instance_id="worker-a", resume_key="new", run=concurrent_write)
