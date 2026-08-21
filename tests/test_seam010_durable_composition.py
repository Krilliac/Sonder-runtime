"""SEAM-010 durable job/workflow composition acceptance tests."""
from __future__ import annotations

import pytest

from sonder_runtime.application.ports.jobs import (
    JobIdentity,
    JobStatus,
    WorkflowDefinition,
    WorkflowStep,
)
from sonder_runtime.domain.common.errors import ConcurrencyConflict
from sonder_runtime.bootstrap import app as bootstrap_app


pytestmark = pytest.mark.integration


def _identity(job_id: str = "workflow-1") -> JobIdentity:
    return JobIdentity(job_id, "workflow", "op-1", "idem-1")


def _definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        "definition-1", (WorkflowStep("step-1", "extract"), WorkflowStep("step-2", "load"))
    )


def test_application_composes_cached_engine_over_shared_durable_job_store(tmp_path, monkeypatch):
    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        first = application.workflow_engine()
        second = application.workflow_engine()

        assert first is second
        assert first._jobs._port is application.job_registry()
        assert database.exists()
        created = first.start(_identity(), _definition())
        assert created.status is JobStatus.PENDING
        assert first.resume("workflow-1", "worker-a").checkpoint.next_step == 0
    finally:
        bootstrap_app.reset_for_tests()


def test_restart_reuses_checkpoint_and_start_is_idempotent(tmp_path, monkeypatch):
    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    first_application = bootstrap_app.build_application()
    first_engine = first_application.workflow_engine()
    first = first_engine.start(_identity(), _definition())
    first_engine.resume("workflow-1", "worker-a", lease_seconds=1)
    checkpoint = first_engine.checkpoint(
        "workflow-1", "worker-a", next_step=1, state={"artifact": "ready"}, completed_step_id="step-1"
    )
    bootstrap_app.reset_for_tests()

    second_engine = bootstrap_app.build_application().workflow_engine()
    replay = second_engine.start(_identity(), _definition())
    second_engine._jobs.reconcile(now="9999-01-01T00:00:00+00:00")
    resumed = second_engine.resume("workflow-1", "worker-b")

    assert replay.identity == first.identity
    assert replay.revision > first.revision
    assert resumed.checkpoint == checkpoint
    bootstrap_app.reset_for_tests()


def test_cancelled_workflow_cannot_resume_and_repeated_cancel_is_idempotent(tmp_path, monkeypatch):
    database = tmp_path / "jobs.db"
    monkeypatch.setenv("SONDER_JOBS_DB", str(database))
    bootstrap_app.reset_for_tests()
    try:
        application = bootstrap_app.build_application()
        engine = application.workflow_engine()
        engine.start(_identity(), _definition())

        first_cancel = application.job_service().cancel("workflow-1", "operator requested stop")
        second_cancel = application.job_service().cancel("workflow-1", "duplicate stop")

        assert first_cancel == second_cancel
        assert first_cancel[0].status is JobStatus.CANCELLED
        with pytest.raises(ConcurrencyConflict):
            engine.resume("workflow-1", "worker-a")
    finally:
        bootstrap_app.reset_for_tests()
