"""Regression coverage for fresh autopilot status snapshots."""
from __future__ import annotations

import os

import pytest

import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store
from sonder_runtime.adapters.persistence.autopilot_repository import AutopilotRepository


@pytest.fixture(autouse=True)
def isolated_autopilot_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    yield
    autopilot_store.reset_schema_cache_for_tests()


def test_original_repository_observes_failure_published_by_second_controller():
    """A repository instance must never retain the initial ``ready`` row."""
    first_controller = AutopilotRepository()
    second_controller = AutopilotRepository()
    created = first_controller.create_run("fail during planning")
    assert first_controller.get_run(created["id"])["status"] == "ready"

    claimed = second_controller.claim_run(
        created["id"], "controller-2", owner_pid=os.getpid(), lease_seconds=300,
    )
    assert claimed["status"] == "planning"
    second_controller.finish_run(
        created["id"], "controller-2", "failed", last_error="planner unavailable",
    )

    current = first_controller.get_run(created["id"])
    assert current["status"] == "failed"
    assert current["last_error"] == "planner unavailable"


@pytest.mark.parametrize("operation", ("start", "resume"))
def test_launch_response_refreshes_after_synchronous_controller_failure(monkeypatch, operation):
    """Launch responses reflect a controller that finishes before return."""
    import server

    owner = AutopilotRepository()
    if operation == "resume":
        created = owner.create_run("resume and fail")
        run_id = created["id"]
    else:
        run_id = None

    def launch(run_id_arg, **_kwargs):
        claimed = owner.claim_run(
            run_id_arg, "synchronous-controller", owner_pid=os.getpid(),
            lease_seconds=300,
        )
        assert claimed is not None
        owner.finish_run(
            run_id_arg, "synchronous-controller", "failed",
            last_error="gateway unavailable",
        )
        return True

    monkeypatch.setattr(server, "_launch_autopilot", launch)
    if operation == "start":
        output = server._autopilot_start("start and fail", allow_web=False)
    else:
        output = server._autopilot_resume(run_id)

    assert "status/phase: failed / failed" in output
    assert "gateway unavailable" in output
