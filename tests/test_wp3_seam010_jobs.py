"""WP3-SEAM-010 contract tests: leased jobs and resumable workflows."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.application.capabilities.jobs import (
    JobRegistryService,
    ResumableWorkflowEngine,
)
from sonder_runtime.application.ports.jobs import (
    JobClaim,
    JobIdentity,
    JobRecord,
    JobStatus,
    WorkflowCheckpoint,
    WorkflowDefinition,
    WorkflowStep,
)
from sonder_runtime.domain.common.errors import ConcurrencyConflict, InvalidInput


def identity(job_id="job-1", *, kind="shell"):
    return JobIdentity(job_id, kind, "op-1", "idem-1")


class FakeJobs:
    def __init__(self):
        self.jobs = {}
        self.claims = {}
        self.reconciled = None

    def create(self, identity, *, metadata=None):
        if identity.job_id in self.jobs:
            raise ConcurrencyConflict("duplicate durable identity")
        job = JobRecord(identity=identity)
        self.jobs[identity.job_id] = job
        return job

    def get(self, job_id):
        return self.jobs.get(job_id)

    def list(self, *, include_terminal=True, limit=100):
        values = tuple(self.jobs.values())
        if not include_terminal:
            values = tuple(job for job in values if not job.is_terminal)
        return values[:limit]

    def claim(self, job_id, worker_id, *, lease_seconds=300):
        job = self.jobs.get(job_id)
        if job is None or job.is_terminal or job_id in self.claims:
            return None
        claim = JobClaim(job_id, worker_id, "lease", job.revision + 1)
        self.claims[job_id] = claim
        self.jobs[job_id] = JobRecord(job.identity, JobStatus.CLAIMED, job.revision + 1)
        return claim

    def heartbeat(self, job_id, worker_id, *, lease_seconds=300):
        claim = self.claims.get(job_id)
        return claim is not None and claim.worker_id == worker_id

    def finish(self, job_id, worker_id, status, *, result=None, error=""):
        claim = self.claims.get(job_id)
        if claim is None or claim.worker_id != worker_id:
            return None
        job = self.jobs[job_id]
        finished = JobRecord(job.identity, status, job.revision + 1, result=result, error=error)
        self.jobs[job_id] = finished
        del self.claims[job_id]
        return finished

    def reconcile(self, *, now=None):
        self.reconciled = now
        stale = len(self.claims)
        self.claims.clear()
        return stale


class FakeCheckpoints:
    def __init__(self):
        self.items = {}

    def get_checkpoint(self, job_id):
        return self.items.get(job_id)

    def save_checkpoint(self, checkpoint, *, expected_sequence):
        current = self.items.get(checkpoint.job_id)
        actual = -1 if current is None else current.sequence
        if actual != expected_sequence:
            return None
        self.items[checkpoint.job_id] = checkpoint
        return checkpoint


def test_durable_identity_requires_stable_nonempty_fields():
    with pytest.raises(ValueError):
        JobIdentity("", "shell", "op", "idem")
    assert identity().idempotency_key == "idem-1"


def test_claim_heartbeat_finish_and_reconcile_are_owner_bound():
    store = FakeJobs()
    registry = JobRegistryService(store)
    registry.create(identity())
    registry.claim("job-1", "worker-a")
    with pytest.raises(ConcurrencyConflict):
        registry.heartbeat("job-1", "worker-b")
    registry.heartbeat("job-1", "worker-a")
    finished = registry.finish("job-1", "worker-a", JobStatus.SUCCEEDED, result={"ok": True})
    assert finished.is_terminal and finished.result == {"ok": True}
    assert registry.reconcile(now="now") == 0


def test_finish_rejects_nonterminal_state_and_invalid_limits():
    registry = JobRegistryService(FakeJobs())
    with pytest.raises(InvalidInput):
        registry.finish("job-1", "worker-a", JobStatus.RUNNING)
    with pytest.raises(InvalidInput):
        registry.list(limit=0)


def test_workflow_resume_and_checkpoint_are_monotonic_and_restart_safe():
    jobs = FakeJobs()
    checkpoints = FakeCheckpoints()
    engine = ResumableWorkflowEngine(JobRegistryService(jobs), checkpoints)
    definition = WorkflowDefinition("wf-1", (WorkflowStep("a", "extract"), WorkflowStep("b", "load")))
    engine.start(identity("job-wf", kind="workflow"), definition)
    resumed = engine.resume("job-wf", "worker-a")
    assert resumed.checkpoint.next_step == 0
    saved = engine.checkpoint("job-wf", "worker-a", next_step=1, state={"artifact": "x"}, completed_step_id="a")
    assert saved.sequence == 1 and saved.next_step == 1
    with pytest.raises(InvalidInput):
        engine.checkpoint("job-wf", "worker-a", next_step=0, state={})


def test_job_status_is_string_serializable_and_terminal_set_is_explicit():
    assert JobStatus.SUCCEEDED.value == "succeeded"
    assert JobStatus.INTERRUPTED not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
