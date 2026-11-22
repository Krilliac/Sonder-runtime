from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.application.jobs.durable_registry import DurableJobRegistry
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus


def _identity(job_id):
    return JobIdentity(job_id, "test", f"op-{job_id}", f"idem-{job_id}")


def test_heartbeat_does_not_erase_cancellation_request(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: "2026-08-20T12:00:00Z")
    registry.create(_identity("j1"))
    assert registry.claim("j1", "w") is not None
    registry.request_cancellation("j1")
    assert registry.heartbeat("j1", "w") is False
    assert registry.poll("j1").status is JobStatus.CANCELLATION_REQUESTED


def test_claim_rejects_cancellation_requested_job(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: "2026-08-20T12:00:00Z")
    registry.create(_identity("j1"))
    registry.request_cancellation("j1")
    assert registry.claim("j1", "w") is None
    assert registry.poll("j1").status is JobStatus.CANCELLATION_REQUESTED


def test_sqlite_recovery_sees_live_jobs_beyond_old_history(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: "2026-08-20T12:00:00Z")
    for i in range(6):
        registry.create(_identity(f"old{i}"))
        registry.transition(f"old{i}", JobStatus.SUCCEEDED)
    registry.create(_identity("live"))
    registry.transition("live", JobStatus.RUNNING)
    plan = registry.reconcile_recovery(max_records=2)
    assert [r.observation.record_id for r in plan.results] == ["live"]
    assert registry.poll("live").status is JobStatus.INTERRUPTED


def test_sqlite_recovery_keeps_cancellation_requested(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: "2026-08-20T12:00:00Z")
    registry.create(_identity("j1"))
    registry.request_cancellation("j1")
    registry.reconcile_recovery()
    assert registry.poll("j1").status is JobStatus.CANCELLATION_REQUESTED


def test_memory_recovery_keeps_cancellation_requested():
    registry = DurableJobRegistry()
    registry.start(_identity("j1"))
    registry.request_cancellation("j1")
    registry.reconcile()
    assert registry.poll("j1").status is JobStatus.CANCELLATION_REQUESTED
