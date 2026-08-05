"""SPEC-3: autopilot_controller.snapshot() migrated onto the AutomationRepository.

The status view no longer calls autopilot_store directly — it goes through the
composition root's automation repository. Behavior must be identical: the same
runs and per-run events, resolved against the same database.
"""
from __future__ import annotations

import pytest

import autopilot_controller
import autopilot_store
from sonder_runtime.bootstrap import app as bootstrap_app


@pytest.fixture()
def autopilot_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    bootstrap_app.reset_for_tests()
    yield
    bootstrap_app.reset_for_tests()
    autopilot_store.reset_schema_cache_for_tests()


def test_snapshot_returns_runs_through_the_port(autopilot_db):
    run = autopilot_store.create_run("ship the feature")
    snap = autopilot_controller.snapshot()
    ids = {r["id"] for r in snap.get("runs", [])}
    assert run["id"] in ids
    # latest + its events are threaded in via the port's events() call.
    assert snap.get("latest", {}).get("id") == run["id"]
    assert any(e.get("kind") == "created" for e in snap.get("events", []))


def test_snapshot_empty_when_no_runs(autopilot_db):
    snap = autopilot_controller.snapshot()
    assert snap.get("runs", []) == []
    assert snap.get("events", []) == []


def test_snapshot_matches_direct_store_shape(autopilot_db):
    autopilot_store.create_run("a")
    autopilot_store.create_run("b")
    via_port = autopilot_controller.snapshot(include_finished=True, limit=20)
    direct = autopilot_store.snapshot(include_finished=True, limit=20)
    # Same run set and counts; the port adds only the events enrichment.
    assert {r["id"] for r in via_port["runs"]} == {r["id"] for r in direct["runs"]}
    assert via_port.get("active_runs") == direct.get("active_runs")
