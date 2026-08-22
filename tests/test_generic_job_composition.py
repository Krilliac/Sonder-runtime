from __future__ import annotations

from pathlib import Path

from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
from sonder_runtime.application.execution.process_jobs import ProcessJobRequest
from sonder_runtime.application.execution.world_control import OutputStream, OutputWatermark
from sonder_runtime.application.ports.jobs import JobIdentity, JobStatus
from sonder_runtime.bootstrap import app as bootstrap_app


class _CompletedProcess:
    pid = 701
    returncode = 0

    def communicate(self, timeout=None):
        return ("small output\n", "large " + ("x" * 20_000))

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def test_composition_exposes_shared_process_provider_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        provider = application.process_job_provider()

        assert isinstance(provider, SubprocessJobProvider)
        assert application.process_job_provider() is provider
        assert application.job_recovery is not None
        assert application.job_service()._process_cleanup is not None
        assert provider._jobs._process_cleanup is application.job_service()._process_cleanup
    finally:
        bootstrap_app.reset_for_tests()


def test_composed_provider_persists_output_watermarks_and_spill_across_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        provider = application.process_job_provider()
        provider._launcher = lambda _argv, **_kwargs: _CompletedProcess()

        started = provider.start(ProcessJobRequest(
            JobIdentity("job-output", "process", "run", "idem-output"),
            ("ignored",),
            cwd=Path(tmp_path),
            max_descendants=4,
        ))
        waited = provider.wait(started.record.identity.job_id)
        assert waited.record.status is JobStatus.SUCCEEDED

        page = application.job_service().stream(
            "job-output", after=OutputWatermark(0), max_events=2, max_bytes=20_000
        )
        assert [event.stream for event in page.events] == [
            OutputStream.STDOUT, OutputStream.STDERR,
        ]
        assert page.events[1].spill is not None
        assert page.next_watermark.sequence == 2
    finally:
        bootstrap_app.reset_for_tests()

    application = bootstrap_app.build_application()
    try:
        record = application.job_service().poll("job-output")
        page = application.job_service().stream("job-output", after=OutputWatermark(1))
        assert record.status is JobStatus.SUCCEEDED
        assert page.events[0].watermark.sequence == 2
        assert page.events[0].spill is not None
    finally:
        bootstrap_app.reset_for_tests()


def test_composed_recovery_marks_orphan_without_claiming_cleanup_for_pending_job(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        application.job_service().start(
            JobIdentity("job-recovery", "process", "run", "idem-recovery")
        )
        report = application.job_recovery(
            owner_instance_id="old-instance",
            owner_alive=False,
            max_records=8,
            max_process_descendants=3,
        )
        assert report.plan.results[0].action.value == "resume"
        assert report.cleanup_receipts == ()
        assert application.job_service().poll("job-recovery").status is JobStatus.PENDING
    finally:
        bootstrap_app.reset_for_tests()
