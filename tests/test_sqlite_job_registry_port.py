from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.adapters.persistence import migrations


def _identity(job_id="job-1"):
    return JobIdentity(job_id, "test", "op-1", f"idem-{job_id}")


def test_job_registry_persists_owner_leases_and_enforces_cas(tmp_path):
    now = ["2026-08-20T12:00:00Z"]
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: now[0])
    registry.create(_identity())

    claim = registry.claim("job-1", "worker-a", lease_seconds=30)
    assert claim is not None
    assert claim.worker_id == "worker-a"
    assert registry.claim("job-1", "worker-b") is None
    assert registry.heartbeat("job-1", "worker-b") is False
    assert registry.heartbeat("job-1", "worker-a", lease_seconds=30) is True
    finished = registry.finish("job-1", "worker-a", JobStatus.SUCCEEDED, result={"ok": True})
    assert finished is not None
    assert finished.status is JobStatus.SUCCEEDED
    assert finished.result == {"ok": True}


def test_expired_lease_can_be_reclaimed_after_restart(tmp_path):
    now = ["2026-08-20T12:00:00Z"]
    path = tmp_path / "jobs.db"
    first = SQLiteDurableJobRegistry(path, clock=lambda: now[0])
    first.create(_identity())
    assert first.claim("job-1", "worker-a", lease_seconds=1) is not None

    reopened = SQLiteDurableJobRegistry(path, clock=lambda: now[0])
    assert reopened.claim("job-1", "worker-b") is None
    now[0] = "2026-08-20T12:00:02Z"
    recovered = reopened.claim("job-1", "worker-b", lease_seconds=10)
    assert recovered is not None
    assert recovered.worker_id == "worker-b"
    assert reopened.finish("job-1", "worker-a", JobStatus.FAILED, error="stale") is None
    assert reopened.finish("job-1", "worker-b", JobStatus.FAILED, error="recovered") is not None


def test_parent_listing_is_declared_by_port_and_survives_reopen(tmp_path):
    path = tmp_path / "jobs.db"
    first = SQLiteDurableJobRegistry(path)
    first.create(_identity("parent"))
    first.create(JobIdentity("child", "test", "op-1", "idem-child", parent_job_id="parent"))

    reopened = SQLiteDurableJobRegistry(path)
    assert [record.identity.job_id for record in reopened.list(parent_job_id="parent")] == ["child"]
    assert reopened.get("child").identity.parent_job_id == "parent"


def test_reconcile_matches_job_registry_port_and_marks_expired_leases(tmp_path):
    now = ["2026-08-20T12:00:00Z"]
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: now[0])
    registry.create(_identity())
    registry.claim("job-1", "worker-a", lease_seconds=1)

    now[0] = "2026-08-20T12:00:02Z"
    assert registry.reconcile(now=now[0]) == 1
    record = registry.get("job-1")
    assert record is not None
    assert record.status is JobStatus.INTERRUPTED
    assert registry.reconcile(now=now[0]) == 0


def test_jobs_store_uses_versioned_adoption_baseline(tmp_path):
    database = tmp_path / "jobs.db"
    SQLiteDurableJobRegistry(database).create(_identity())

    before = migrations.status("jobs", str(database))
    assert before.pending == ("0001_baseline",)

    after = migrations.migrate_store("jobs", str(database))
    assert after.current
    assert after.applied == ("0001_baseline",)
