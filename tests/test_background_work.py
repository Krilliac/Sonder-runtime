from types import SimpleNamespace
import json
import os

import pytest

from sonder_runtime.interfaces.http.background_work import (
    BackgroundWorkAggregator,
    build_background_work_aggregator,
    dispatch_background_work_route,
    handle_background_work_request,
    runtime_background_work,
)


def _aggregator(lanes=None, fleets=None, autopilot=None):
    return BackgroundWorkAggregator(
        lanes=lanes or (lambda context, **kwargs: {"lanes": []}),
        fleets=fleets or (lambda **kwargs: []),
        autopilot=autopilot or (lambda **kwargs: []),
        owner_scope=lambda context: f"owner:{context.principal_id}",
        clock=lambda: 110.0,
    )


def test_aggregate_groups_newest_first_and_expands_fleet_children():
    lanes = lambda context, **kwargs: {"lanes": [
        {"id": "lane-old", "task": "old", "status": "completed", "updated_ts": 101},
        {"id": "lane-new", "task": "new", "status": "running", "updated_ts": 109, "started_ts": 108},
    ]}
    fleets = lambda **kwargs: [
        {"id": "master-1", "role": "master", "task": "build", "status": "running",
         "requested_agents": 2, "worker_slots": 1, "started_ts": 100, "updated_ts": 107},
        {"id": "child-1", "parent_id": "master-1", "task": "a", "status": "done",
         "updated_ts": 105, "summary": "first result"},
        {"id": "child-2", "parent_id": "master-1", "task": "b", "status": "running",
         "updated_ts": 106, "activity": "working"},
    ]
    autopilot = lambda **kwargs: [{
        "id": "auto-1", "objective": "goal", "status": "running", "phase": "execute",
        "current_task": 0, "plan": [{"status": "done", "task": "step one"}],
        "updated_ts": 103,
    }]
    result = _aggregator(lanes, fleets, autopilot).snapshot(SimpleNamespace(principal_id="p"))
    assert [item["id"] for item in result["groups"]["lanes"]] == ["lane-new", "lane-old"]
    fleet = result["groups"]["fleets"][0]
    assert fleet["worker_slots"] == 1
    assert fleet["counts"]["done"] == 1
    assert [child["id"] for child in fleet["children"]] == ["child-2", "child-1"]
    assert fleet["children"][1]["preview"] == "first result"
    assert result["groups"]["autopilot"][0]["task_counts"]["done"] == 1
    assert result["groups"]["autopilot"][0]["current_task"] == "step one"


def test_builder_keeps_provider_wiring_explicit():
    aggregate = build_background_work_aggregator(
        lanes=lambda context, **kwargs: {"lanes": []},
        fleets=lambda **kwargs: [],
        autopilot=lambda **kwargs: [],
        owner_scope=lambda context: "owner:p",
        clock=lambda: 12.0,
    )
    result = aggregate.snapshot(SimpleNamespace(principal_id="p"))
    assert result["captured_at"] == 12.0


def test_aggregate_requires_authenticated_owner_and_scopes_providers():
    seen = {}
    def fleets(**kwargs):
        seen.update(kwargs)
        return []
    aggregate = _aggregator(fleets=fleets)
    with pytest.raises(PermissionError):
        aggregate.snapshot(SimpleNamespace(principal_id=""))
    aggregate.snapshot(SimpleNamespace(principal_id="principal"), project="project-a")
    assert seen == {"owner_id": "owner:principal", "project": "project-a", "limit": 100}


def test_http_route_is_read_only_and_validates_query():
    aggregate = _aggregator()
    context = SimpleNamespace(principal_id="p")
    assert dispatch_background_work_route(aggregate, "POST", "/v1/background-work", context=context).status_code == 405
    assert dispatch_background_work_route(aggregate, "GET", "/v1/other", context=context) is None
    response = dispatch_background_work_route(
        aggregate, "GET", "/v1/background-work", context=context, query={"limit": "1"}
    )
    assert response.status_code == 200
    assert response.body["groups"] == {"lanes": [], "fleets": [], "autopilot": []}


def test_host_adapter_marks_paged_and_malformed_provider_data_truthfully():
    aggregate = _aggregator(
        lanes=lambda context, **kwargs: {"lanes": [None, {"id": "lane-1"}], "has_more": True},
        fleets=lambda **kwargs: {"agents": [None], "truncated": True},
        autopilot=lambda **kwargs: {"runs": [None], "has_more": True},
    )
    host = SimpleNamespace(_background_work_aggregator=aggregate)
    response = handle_background_work_request(
        host, "GET", "/v1/background-work", context=SimpleNamespace(principal_id="p")
    )
    assert response.status_code == 200
    assert response.body["groups"]["lanes"][0]["id"] == "lane-1"
    assert response.body["truncated"] == {"lanes": True, "fleets": True, "autopilot": True}
    assert handle_background_work_request(
        SimpleNamespace(), "GET", "/v1/background-work",
        context=SimpleNamespace(principal_id="p"),
    ) is None


def test_aggregate_accepts_persisted_json_and_separate_masters_without_duplicates():
    result = _aggregator(
        fleets=lambda **kwargs: {
            "masters": [{
                "id": "master-1", "role": "master", "task": "fleet",
                "status": "running", "requested_agents": "bad",
                "worker_slots": "2", "updated_ts": 20,
            }],
            "agents": [
                {"id": "master-1", "role": "master", "status": "running"},
                {"id": "child-1", "parent_id": "master-1", "status": "queued"},
            ],
        },
        autopilot=lambda **kwargs: [{
            "id": "auto-1", "status": "running", "phase": "plan",
            "current_task": 0, "plan_json": '[{"status":"running","title":"write"}]',
        }],
    ).snapshot(SimpleNamespace(principal_id="p"))
    fleet = result["groups"]["fleets"][0]
    assert fleet["id"] == "master-1"
    assert fleet["requested_agents"] == 1
    assert fleet["worker_slots"] == 2
    assert [child["id"] for child in fleet["children"]] == ["child-1"]
    assert result["groups"]["autopilot"][0]["current_task"] == "write"


def test_real_stores_show_48_children_even_with_one_master_page(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence import fleet_store, autopilot_store
    from sonder_runtime.adapters.persistence.background_work import fleet_snapshot, autopilot_snapshot

    monkeypatch.setenv("SONDER_FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    fleet_store.create_agent({"id": "old-master", "role": "master", "task": "older", "status": "done",
                              "started_ts": 10, "updated_ts": 2000}, "process", os.getpid())
    fleet_store.create_agent({"id": "master-48", "role": "master", "task": "48-agent work", "status": "running",
                              "requested_agents": 48, "worker_slots": 1, "started_ts": 100, "updated_ts": 100},
                             "process", os.getpid())
    for index in range(48):
        fleet_store.create_agent({"id": f"child-{index}", "parent_id": "master-48", "task": f"part {index}",
                                  "status": "done" if index < 24 else "running" if index == 24 else "queued",
                                  "summary": "result preview", "started_ts": 101 + index, "updated_ts": 101 + index},
                                 "process", os.getpid())
    run = autopilot_store.create_run("autopilot goal", request_owner="caller")
    with autopilot_store._write_transaction() as connection:
        connection.execute("UPDATE autopilot_runs SET status='paused', phase='execute', current_task=1, plan_json=? WHERE id=?",
                           (json.dumps([{"task": "done task", "status": "passed"},
                                        {"task": "current task", "status": "running"}]), run["id"]))
    app = SimpleNamespace(agent_lanes=lambda: SimpleNamespace(list=lambda *a, **kw: {"lanes": []}))
    aggregate = runtime_background_work(app, {}, fleet_snapshot=fleet_snapshot,
                                       autopilot_snapshot=autopilot_snapshot,
                                       admin_authorized=lambda auth: True, request_owner=lambda auth: None)
    snapshot = aggregate.snapshot(SimpleNamespace(principal_id="owner"), limit=1)
    fleet = snapshot["groups"]["fleets"][0]
    assert fleet["id"] == "master-48"  # Newest creation, not most recently updated.
    assert fleet["requested_agents"] == 48 and fleet["worker_slots"] == 1
    assert len(fleet["children"]) == 48
    assert fleet["counts"] == {"done": 24, "running": 1, "queued": 23, "failed": 0, "cancelled": 0, "other": 0}
    assert all(child["preview"] == "result preview" for child in fleet["children"])
    assert snapshot["truncated"]["fleets"] is True
    auto = snapshot["groups"]["autopilot"][0]
    assert auto["id"] == run["id"]
    assert auto["current_task"] == "current task"
    assert auto["task_counts"]["done"] == 1
    assert auto["phase"] == "execute"
    # Status reads must not reconcile deliberately old fixture process leases.
    assert fleet_store.get_agent("master-48")["status"] == "running"


def test_real_autopilot_owner_isolation_and_terminal_retention(tmp_path, monkeypatch):
    from sonder_runtime.adapters.persistence import autopilot_store
    from sonder_runtime.adapters.persistence.background_work import autopilot_snapshot

    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    ours = autopilot_store.create_run("ours", request_owner="ta-account-a")
    theirs = autopilot_store.create_run("theirs", request_owner="ta-account-b")
    autopilot_store.create_run("legacy local", request_owner="")
    with autopilot_store._write_transaction() as connection:
        connection.execute("UPDATE autopilot_runs SET status='completed', created_ts=100, updated_ts=200 WHERE id=?", (ours["id"],))
    app = SimpleNamespace(agent_lanes=lambda: SimpleNamespace(list=lambda *a, **kw: {"lanes": []}))
    aggregate = runtime_background_work(app, {},
        fleet_snapshot=lambda **kw: pytest.fail("non-admin accessed global fleet store"),
        autopilot_snapshot=autopilot_snapshot, admin_authorized=lambda auth: False,
        request_owner=lambda auth: "ta-account-a")
    result = aggregate.snapshot(SimpleNamespace(principal_id="account:a"))
    assert result["groups"]["fleets"] == []
    assert [row["id"] for row in result["groups"]["autopilot"]] == [ours["id"]]
    assert result["groups"]["autopilot"][0]["elapsed_seconds"] == 100
    assert result["groups"]["autopilot"][0]["cancelable"] is False
    assert theirs["id"] not in str(result)


def test_missing_lane_service_does_not_hide_existing_fleets():
    # Runtime graphs normally have all three providers; if lane composition
    # fails, fail visibly instead of reporting a false empty background page.
    aggregate = runtime_background_work(SimpleNamespace(), {},
        fleet_snapshot=lambda **kw: [], autopilot_snapshot=lambda **kw: [],
        admin_authorized=lambda auth: True, request_owner=lambda auth: None)
    with pytest.raises(AttributeError):
        aggregate.snapshot(SimpleNamespace(principal_id="owner"))
