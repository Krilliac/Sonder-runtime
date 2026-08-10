"""SPEC-3: LegacyAutomationRepository faithfully wraps autopilot_store.

The strangler adapter must delegate to the root store without changing its
observable behavior, must hold no state (so it is safe to build eagerly in the
composition root), and must be the run ledger the assembled Application exposes.
"""
from __future__ import annotations

import os

import pytest

import autopilot_store
from sonder_runtime.adapters.strangler_services import LegacyAutomationRepository
from sonder_runtime.bootstrap import app as bootstrap_app


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    yield LegacyAutomationRepository()
    autopilot_store.reset_schema_cache_for_tests()


def test_constructing_the_adapter_opens_no_database(tmp_path, monkeypatch):
    # Imports and the connection live inside the methods, so building the
    # adapter (as the composition root does) must not touch the disk.
    db = tmp_path / "autopilot.db"
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(db))
    autopilot_store.reset_schema_cache_for_tests()
    LegacyAutomationRepository()
    assert not db.exists()


def test_create_then_get_round_trip(repo):
    run = repo.create_run("ship the thing", project="/repo", tier="code")
    assert run["status"] == "ready"
    assert run["objective"] == "ship the thing"
    fetched = repo.get_run(run["id"])
    assert fetched is not None
    assert fetched["id"] == run["id"]


def test_claim_heartbeat_progress_finish_lifecycle(repo):
    run = repo.create_run("do work")
    owner = "owner-1"
    claimed = repo.claim_run(run["id"], owner, owner_pid=os.getpid())
    assert claimed is not None
    assert claimed["owner_id"] == owner
    assert repo.heartbeat(run["id"], owner) is True
    updated = repo.save_progress(run["id"], owner, phase="execute", cycles_delta=1)
    assert updated is not None
    finished = repo.finish_run(
        run["id"], owner, "completed", summary="done", final_report="ok"
    )
    assert finished is not None
    assert finished["status"] == "completed"


def test_claim_refuses_a_finished_run(repo):
    run = repo.create_run("short")
    owner = "owner-1"
    repo.claim_run(run["id"], owner, owner_pid=os.getpid())
    repo.finish_run(run["id"], owner, "completed")
    # A terminal run can no longer be claimed.
    assert repo.claim_run(run["id"], "owner-2", owner_pid=os.getpid()) is None


def test_request_pause_and_cancel_set_control_flags(repo):
    run = repo.create_run("pausable")
    owner = "owner-1"
    repo.claim_run(run["id"], owner, owner_pid=os.getpid())
    repo.request_pause(run["id"])
    assert repo.control_flags(run["id"], owner)["pause"] is True
    repo.request_cancel(run["id"])
    assert repo.control_flags(run["id"], owner)["cancel"] is True


def test_list_runs_and_snapshot_and_events(repo):
    a = repo.create_run("first")
    repo.create_run("second")
    runs = repo.list_runs()
    assert len(runs) >= 2
    snap = repo.snapshot()
    assert "runs" in snap
    evts = repo.events(a["id"])
    assert any(e.get("kind") == "created" for e in evts)


def test_reconcile_stale_runs_returns_a_count(repo):
    repo.create_run("orphan")
    # No live owner claimed anything; reconciliation is a no-op count, never raises.
    assert isinstance(repo.reconcile_stale_runs(), int)


def test_application_exposes_the_automation_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy.json"))
    autopilot_store.reset_schema_cache_for_tests()
    bootstrap_app.reset_for_tests()
    app = bootstrap_app.build_application()
    assert isinstance(app.automation, LegacyAutomationRepository)
    run = app.automation.create_run("through the graph")
    assert app.automation.get_run(run["id"])["objective"] == "through the graph"
    bootstrap_app.reset_for_tests()
    autopilot_store.reset_schema_cache_for_tests()
