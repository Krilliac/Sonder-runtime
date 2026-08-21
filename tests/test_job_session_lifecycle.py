from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.execution.world_control import OutputEvent, OutputStream, OutputWatermark
from sonder_runtime.application.jobs.session_lifecycle import (
    JobRegistryLifecycleAdapter,
    JobSessionLifecycleRecorder,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus


def _record(revision=0, status=JobStatus.PENDING):
    return JobRecord(JobIdentity("job-1", "test", "op-1", "idem-1", parent_session_id="session-1"), status, revision)


def test_lifecycle_and_output_are_idempotent_and_reopenable(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    recorder = JobSessionLifecycleRecorder(repo)

    created = recorder.record_lifecycle(_record())
    output = recorder.record_output(_record(), OutputEvent(OutputWatermark(1), OutputStream.STDOUT, "ready"))
    finished = recorder.record_lifecycle(_record(1, JobStatus.SUCCEEDED))
    assert created and output and finished
    assert not created.replayed
    assert not output.replayed
    assert not finished.replayed

    reopened = JobSessionLifecycleRecorder(repo)
    assert reopened.record_lifecycle(_record()).replayed
    assert reopened.record_output(_record(), OutputEvent(OutputWatermark(1), OutputStream.STDOUT, "ready")).replayed
    assert [event.event_type for event in reopened.replay("session-1", job_id="job-1")] == [
        "job.created", "job.output", "job.lifecycle",
    ]


def test_unlinked_jobs_are_noops_and_output_is_bounded(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    recorder = JobSessionLifecycleRecorder(repo, max_output_bytes=4)
    unlinked = JobRecord(JobIdentity("job-2", "test", "op-2", "idem-2"))
    assert recorder.record_lifecycle(unlinked) is None
    assert recorder.record_output(unlinked, OutputEvent(OutputWatermark(1), OutputStream.STDOUT, "ready")) is None
    linked = _record()
    try:
        recorder.record_output(linked, OutputEvent(OutputWatermark(1), OutputStream.STDOUT, "ready!"))
    except ValueError as exc:
        assert "bound" in str(exc)
    else:
        raise AssertionError("oversized output must be rejected")


def test_registry_adapter_preserves_order_and_idempotency(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    adapter = JobRegistryLifecycleAdapter(JobSessionLifecycleRecorder(repo))
    records = (_record(), _record(1, JobStatus.SUCCEEDED))

    first = adapter.record_many(records)
    second = adapter.record_many(records)

    assert [linkage.event_key for linkage in first] == [
        "job-1:revision:0", "job-1:revision:1",
    ]
    assert all(not linkage.replayed for linkage in first)
    assert all(linkage.replayed for linkage in second)
