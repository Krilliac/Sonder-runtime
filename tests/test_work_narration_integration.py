"""Isolated integration coverage for narration composition seams.

These tests intentionally avoid the repository conftest and all filesystem,
server, and live-store setup.  They pin owner-link filtering at the bootstrap
boundary using fake store snapshots.
"""
from __future__ import annotations

import sonder_runtime.bootstrap.work_narration as bootstrap
from sonder_runtime.application.ports import work_narration as port


def test_prepare_ack_includes_capacity_reason_and_controls():
    class Orchestrator:
        def max_agents(self): return 7
        def capacity(self, agents): return {"worker_slots": 3, "requested_agents": agents}

    text = bootstrap.prepare_ack(
        {"master_orchestrator": Orchestrator()}, "build the project", "fleet",
        project="D:/project", reason="three independent work items", run_id="fleet-1",
    )
    assert "fleet of 7 agents on 3 worker slots" in text
    assert "three independent work items" in text
    assert "/agents" in text and "/agentcancel <master-id>" in text


def test_enrich_projects_only_owner_linked_sources_and_delayed_child_completion(monkeypatch):
    monkeypatch.setattr(bootstrap.fleet_store, "get_agent", lambda ident: {
        "id": ident, "owner_id": "owner", "project": "repo",
    } if ident == "root" else {"id": ident})
    agents = [
        {"id": "root", "role": "master", "status": "running", "updated_ts": 10},
        {"id": "child", "parent_id": "root", "role": "worker", "status": "running",
         "updated_ts": 10, "activity": "running"},
    ]
    monkeypatch.setattr(bootstrap.fleet_store, "list_agents_scoped", lambda *args, **kwargs: agents)
    monkeypatch.setattr(bootstrap.fleet_store, "events_for_agents", lambda ids: [
        e for e in [{"agent_id": "child", "ts": 10, "message": "started"},
                   {"agent_id": "unrelated", "ts": 99, "message": "leak"}]
        if e["agent_id"] in ids
    ])
    monkeypatch.setattr(bootstrap.autopilot_store, "get_run", lambda ident: None)
    monkeypatch.setattr(bootstrap.fanout_store, "get_run", lambda ident: None)
    monkeypatch.setattr(bootstrap.activity_tracker, "snapshot", lambda: {"active": [], "latest": None, "event_ring": []})

    record = {"id": "wr-1", "status": "running", "created_at": 1,
              "narration": {"links": [{"kind": "fleet", "id": "root"}]}}
    first = bootstrap.enrich_work_record(record)
    assert any("started" in row["text"] for row in first["progress"])
    assert all("leak" not in row["text"] for row in first["progress"])
    assert first["progress_complete"] is False

    agents[0] = dict(agents[0], status="done", finished_ts=20)
    agents[1] = dict(agents[1], status="done", finished_ts=20, summary="child output")
    monkeypatch.setattr(bootstrap.fleet_store, "list_agents_scoped", lambda *args, **kwargs: agents)
    monkeypatch.setattr(bootstrap.fleet_store, "get_agent", lambda ident: {
        "id": ident, "owner_id": "owner", "project": "repo", "status": "done",
    } if ident == "root" else {"id": ident})
    complete = dict(record, status="returned", updated_at=20)
    second = bootstrap.enrich_work_record(complete)
    assert second["progress_complete"] is True
    assert second["final_summary"]


def test_enrich_does_not_claim_verified_without_receipt(monkeypatch):
    monkeypatch.setattr(bootstrap.fleet_store, "get_agent", lambda ident: {"id": ident, "owner_id": "o", "project": "p"})
    monkeypatch.setattr(bootstrap.fleet_store, "list_agents_scoped", lambda *a, **k: [])
    monkeypatch.setattr(bootstrap.fleet_store, "events_for_agents", lambda ids: [])
    monkeypatch.setattr(bootstrap.autopilot_store, "get_run", lambda ident: None)
    monkeypatch.setattr(bootstrap.fanout_store, "get_run", lambda ident: None)
    monkeypatch.setattr(bootstrap.activity_tracker, "snapshot", lambda: {"active": [], "latest": None, "event_ring": []})
    result = bootstrap.enrich_work_record({"id": "wr-2", "status": "returned", "output": "model prose",
                                           "narration": {"links": [{"kind": "fleet", "id": "root"}]}})
    assert "verified" not in result["final_summary"].lower()
    assert "validation receipt" in result["final_summary"]


def test_cancel_linked_work_calls_exact_host_ids_only(monkeypatch):
    calls = []
    monkeypatch.setattr(bootstrap.fleet_store, "get_agent", lambda ident: {"id": ident} if ident == "fleet-1" else None)
    monkeypatch.setattr(bootstrap.fleet_store, "cancel_agents", lambda ident: calls.append(("fleet", ident)))
    monkeypatch.setattr(bootstrap.autopilot_store, "get_run", lambda ident: {"id": ident} if ident == "auto-1" else None)
    monkeypatch.setattr(bootstrap.autopilot_store, "request_cancel", lambda ident: calls.append(("auto", ident)))
    monkeypatch.setattr(bootstrap.fanout_store, "get_run", lambda ident: {"id": ident} if ident == "fan-1" else None)
    monkeypatch.setattr(bootstrap.fanout_store, "request_cancel", lambda ident: calls.append(("fan", ident)))
    result = bootstrap.cancel_linked_work({"id": "wr-3", "narration": {"links": [
        {"kind": "fleet", "id": "fleet-1"}, {"kind": "autopilot", "id": "auto-1"},
        {"kind": "fanout", "id": "fan-1"}, {"kind": "fleet", "id": "not-authorized"},
    ]}})
    assert calls == [("fleet", "fleet-1"), ("auto", "auto-1"), ("fan", "fan-1")]
    assert result["cancel_requested"] is True


def test_narrated_writes_ack_before_work_and_is_inert_without_writer(monkeypatch):
    monkeypatch.setattr(bootstrap, "_watch", lambda *args: None)
    order = []

    @bootstrap.narrated("autopilot", lambda: {})
    def work(task="do it", mode="autopilot"):
        order.append("work")
        return "done"

    with port.output(lambda text: order.append(text)):
        assert work() == "done"
    assert order[0].startswith("I will ") and order[-1] == "work"

    order.clear()
    assert work() == "done"
    assert order == ["work"]


def test_scoped_background_activity_survives_another_latest_response(monkeypatch):
    from sonder_runtime.bootstrap.http_work_narration import run_scoped
    tracker = bootstrap.activity_tracker
    tracker.reset_for_tests()
    links = []
    def work():
        tracker.record_event("tool_result", ok=True, summary="created report.txt TOKEN=private-value")
    run_scoped("wr-owner", lambda rid, kind, child: links.append({"kind": kind, "id": child}), "plan", "", work)
    with tracker.response_span("different conversation"):
        tracker.record_event("tool_result", summary="other owner's output")
    record = {"id": "wr-owner", "status": "returned", "narration": {"links": links}}
    detailed = bootstrap.enrich_work_record(record, include_detail=True)
    assert any("report.txt" in row["text"] for row in detailed["progress"])
    assert all("private-value" not in row["text"] and "other owner" not in row["text"]
               for row in detailed["progress"])
    redacted = bootstrap.enrich_work_record(record)
    assert all("report.txt" not in row["text"] for row in redacted["progress"])
    tracker.reset_for_tests()


def test_activity_ids_from_an_earlier_process_are_not_rebound(monkeypatch):
    monkeypatch.setattr(bootstrap.activity_tracker, "snapshot", lambda: {
        "active": [{"id": "r000001", "events": [{"kind": "tool_result", "summary": "private"}]}],
    })
    result = bootstrap.enrich_work_record({"id": "wr-x", "status": "running", "narration": {
        "activity_id": "an-old-process:r000001",
    }}, include_detail=True)
    assert result["progress"] == []


def test_snapshot_progress_is_additive_and_empty_plain_status_is_unchanged():
    payload = {"models": ["local"], "status": "ready", "activity": {"active": []}}
    result = bootstrap.enrich_status(payload)
    assert result == {**payload, "progress": []}
    assert "progress" not in payload


def test_combined_children_share_one_work_run_rate_limit():
    from sonder_runtime.domain.work_narration import bounded_progress
    rows = [{"id": str(i), "run_id": rid, "at": at, "text": str(i), "kind": kind, "final": final}
            for i, (rid, at, kind, final) in enumerate([
                ("fleet", 1, "task_started", False), ("activity", 4, "tool_start", False),
                ("fleet", 5, "task_finished", True), ("activity", 16, "tool_start", False),
            ])]
    result = bounded_progress(rows, "wr-origin")
    assert [row["id"] for row in result] == ["0", "2", "3"]
    assert {row["run_id"] for row in result} == {"wr-origin"}


def test_repl_error_stays_failed_for_the_final_progress_summary(monkeypatch):
    import pytest
    states, output = [], []
    monkeypatch.setattr(bootstrap, "_watch", lambda state, *_: states.append(state))
    @bootstrap.narrated("workbench", lambda: {})
    def work(prompt):
        raise RuntimeError("provider offline")
    with port.output(output.append), pytest.raises(RuntimeError, match="provider offline"):
        work("inspect project")
    assert output[0].startswith("I will ")
    assert states[0].outcome == "failed"
    assert output[-1].startswith("Work failed")
