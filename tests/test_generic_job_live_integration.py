"""Focused live-composition coverage for the bounded generic job surface."""
from __future__ import annotations

from sonder_runtime.application.execution.world_control import (
    OutputStream,
    OutputWatermark,
    SpillReference,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.application.jobs.durable_registry import ProcessTreeCleanupReceipt
from sonder_runtime.bootstrap import app as bootstrap_app


class _Supervisor:
    def __init__(self) -> None:
        self.requests = []

    def cleanup(self, request):
        self.requests.append(request)
        return ProcessTreeCleanupReceipt(
            request.job_id,
            True,
            descendants_seen=1,
            descendants_terminated=1,
            complete=True,
        )


def test_live_composition_starts_polls_streams_and_links_bounded_output(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_JOBS_DB", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(tmp_path / "sessions.db"))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        service = application.job_service()
        identity = JobIdentity(
            "generic-live",
            "generic",
            "op-live",
            "idem-live",
            parent_session_id="session-live",
        )

        started = service.start(identity)
        assert service.poll(started.identity.job_id).status is JobStatus.PENDING
        service.append_output(
            started.identity.job_id,
            OutputStream.STDOUT,
            "inline",
            spill=SpillReference("a" * 64, "large preview", 123, "text/plain", "generic-live"),
        )
        page = service.stream(started.identity.job_id, after=OutputWatermark(0))
        assert page.events[0].data == "inline"
        assert page.events[0].spill is not None

        events = application.session_repository().read_range("session-live", limit=10)
        assert [event.event_type for event in events] == ["job.created", "job.output"]
    finally:
        bootstrap_app.reset_for_tests()


def test_live_composition_recovery_cleans_orphan_and_persists_interrupted_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_JOBS_DB", str(tmp_path / "jobs.db"))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        service = application.job_service()
        supervisor = _Supervisor()
        service._process_cleanup = supervisor
        service.start(
            JobIdentity("generic-orphan", "generic", "op-recover", "idem-recover"),
            process_id=4242,
            process_group_id=4242,
        )
        application.job_registry().transition("generic-orphan", JobStatus.RUNNING)

        report = service.recover(
            owner_instance_id="old-instance",
            owner_alive=False,
            max_process_descendants=3,
        )

        assert report.interrupted_job_ids == ("generic-orphan",)
        assert supervisor.requests[0].max_descendants == 3
        assert service.poll("generic-orphan").status is JobStatus.INTERRUPTED
    finally:
        bootstrap_app.reset_for_tests()
