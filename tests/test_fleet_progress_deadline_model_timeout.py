"""The fleet progress deadline is derived from the per-call model timeout.

A fixed 120s progress deadline was shorter than the 300s ``SONDER_TIMEOUT``
default.  A lane inside one model call does not touch its fleet row until the
call returns, so on a CPU host every fleet whose calls took over two minutes
was declared "stalled" while each call was still inside its own timeout, and
the late results were discarded.
"""
import os
import threading

import pytest

import master_orchestrator
import sonder_runtime.adapters.persistence.fleet_store as fleet_store
from sonder_runtime.domain.adaptive_concurrency import LaneClaim, ResourceSnapshot


def setup_function():
    master_orchestrator.reset_for_tests()


def _scheduler(claims):
    return master_orchestrator.AdaptiveLaneScheduler(
        claims, 1, resources=lambda: ResourceSnapshot(),
    )


def _isolated_store(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FLEET_DB", str(tmp_path / "fleet.db"))
    fleet_store.reset_schema_cache_for_tests()
    fleet_store.clear_all()


def _row(agent_id):
    return {
        "id": agent_id, "role": "agent", "parent_id": "", "task": "work",
        "status": "queued", "activity": "queued", "started_ts": 100.0,
        "updated_ts": 100.0, "tokens_in": 0, "files": [],
    }


def test_default_deadline_exceeds_the_model_timeout_plus_margin(monkeypatch):
    monkeypatch.delenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", raising=False)
    monkeypatch.delenv("SONDER_TIMEOUT", raising=False)
    assert fleet_store.progress_deadline_seconds() == 300 + 60

    monkeypatch.setenv("SONDER_TIMEOUT", "900")
    assert fleet_store.progress_deadline_seconds() == 900 + 60

    # A short model timeout never lowers the historical floor.
    monkeypatch.setenv("SONDER_TIMEOUT", "30")
    assert fleet_store.progress_deadline_seconds() == 120


def test_explicit_deadline_is_honoured_but_not_inside_a_model_call(monkeypatch):
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "5")
    monkeypatch.setenv("SONDER_TIMEOUT", "300")
    assert fleet_store.progress_deadline_seconds() == 5
    assert fleet_store.lane_progress_deadline({"in_model_call": 0}, 5) == 5
    assert fleet_store.lane_progress_deadline({"in_model_call": 1}, 5) == 360


def test_snapshot_does_not_mark_a_lane_inside_its_model_call_stalled(
        monkeypatch, tmp_path):
    _isolated_store(monkeypatch, tmp_path)
    monkeypatch.delenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", raising=False)
    monkeypatch.delenv("SONDER_TIMEOUT", raising=False)
    clock = {"now": 100.0}
    monkeypatch.setattr(fleet_store.time, "time", lambda: clock["now"])
    fleet_store.register_owner("owner-call", os.getpid(), 100.0)
    fleet_store.create_agent(_row("agent-call"), "owner-call", os.getpid())
    fleet_store.start_agent("agent-call", "owner-call", "running")
    fleet_store.begin_model_call("agent-call", "owner-call", "calling model", tool_calls=0)

    # 200s into a call with a 300s timeout: slow, not stalled.
    clock["now"] = 300.0
    snapshot = fleet_store.snapshot(include_finished=False)
    assert snapshot["agents"][0]["stalled"] is False
    assert snapshot["stalled_agent_total"] == 0

    # Past the call's own timeout plus margin: stalled.
    clock["now"] = 100.0 + 361.0
    snapshot = fleet_store.snapshot(include_finished=False)
    assert snapshot["agents"][0]["stalled"] is True
    assert snapshot["stalled_agent_total"] == 1


def test_dispatch_keeps_a_lane_inside_its_model_call_alive(monkeypatch):
    # A deliberately tiny general deadline: before the fix this lane was
    # declared stalled after 0.1s although its model call had a 300s budget.
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "0.1")
    monkeypatch.setenv("SONDER_TIMEOUT", "300")
    monkeypatch.setattr(
        fleet_store, "get_agent",
        lambda _lane: {"updated_ts": 1.0, "in_model_call": 1},
    )
    scheduler = _scheduler([LaneClaim("slow")])
    release = threading.Event()

    def run_lane(_lane_id, _sink):
        release.wait(0.8)
        return "late but valid"

    collected = {}
    errors = {}
    master_orchestrator.dispatch_lanes(
        scheduler, 1, run_lane,
        lambda lane, result: collected.__setitem__(lane, result),
        lambda lane, exc: errors.__setitem__(lane, str(exc)),
        on_stall=lambda lanes: pytest.fail("lane in a model call was declared stalled"),
    )

    assert collected == {"slow": "late but valid"}
    assert errors == {}


def test_dispatch_still_stalls_a_lane_outside_a_model_call(monkeypatch):
    monkeypatch.setenv("SONDER_FLEET_PROGRESS_DEADLINE_SECONDS", "0.1")
    monkeypatch.setattr(
        fleet_store, "get_agent",
        lambda _lane: {"updated_ts": 1.0, "in_model_call": 0},
    )
    scheduler = _scheduler([LaneClaim("hung")])
    release = threading.Event()
    stalled = []

    def run_lane(_lane_id, _sink):
        release.wait(5)
        return "late"

    try:
        with pytest.raises(master_orchestrator.FleetStalledError):
            master_orchestrator.dispatch_lanes(
                scheduler, 1, run_lane, lambda *_a: None, lambda *_a: None,
                on_stall=stalled.extend,
            )
    finally:
        release.set()
    assert stalled == ["hung"]
