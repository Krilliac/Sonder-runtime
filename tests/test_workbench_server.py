import json

import activity_tracker
import memory_store
import server


def _inventory_result(root="workspace"):
    return {
        "root": root,
        "files": 3,
        "directories": 1,
        "bytes": 123,
        "entries_scanned": 4,
        "elapsed_ms": 2,
        "skipped_entries": 1,
        "skipped_by_reason": {"ignored_directory": 1},
        "skipped_examples": [],
        "truncated": False,
        "truncation_reason": "",
        "manifests": ["pyproject.toml"],
        "extensions": [{"extension": ".py", "files": 2, "bytes": 100}],
        "largest_files": [{"relative": "server.py", "bytes": 80}],
        "top_areas": [{"path": ".", "files": 3, "bytes": 123}],
        "files_seen": 3,
        "directories_seen": 1,
    }


def test_workspace_inventory_is_exposed_to_commands_agents_and_activity(monkeypatch):
    activity_tracker.reset_for_tests()
    calls = []

    def fake_inventory(path, **kwargs):
        calls.append((path, kwargs))
        return _inventory_result(path)

    monkeypatch.setattr(server.workbench, "workspace_inventory", fake_inventory)

    with activity_tracker.response_span("inventory", "inspect workspace"):
        output = server.control_command("/inventory src")
    dispatched = server._agent_dispatch(
        "workspace_inventory",
        {"path": "src", "max_entries": 99, "timeout_seconds": 1},
    )

    assert "workspace inventory: src" in output
    assert "pyproject.toml" in output
    assert "workspace inventory: src" in dispatched
    assert calls[-1][1]["max_entries"] == 99
    actions = [
        row for row in activity_tracker.latest()["events"]
        if row.get("kind") == "tool_call"
    ]
    assert actions[0]["title"] == "Inventoried Workspace"


def test_checklist_lifecycle_persists_order_and_parent_status(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "checklist.db"))

    created = server.checklist_create(
        "Ship workbench",
        json.dumps(["Inspect files", {"title": "Run tests", "detail": "focused"}]),
        project="trilobite",
    )
    checklist_id = created.splitlines()[0].rsplit(" ", 1)[-1]

    first = server.checklist_update(checklist_id, "1", "done", "inspected")
    final = server.checklist_update(checklist_id, "2", "done", "passed")

    assert "[x] 1. Inspect files" in first
    assert "[done] 2/2 complete" in final
    assert final.index("Inspect files") < final.index("Run tests")


def test_checklist_rejects_all_invalid_items_before_writing(monkeypatch, tmp_path):
    db_path = tmp_path / "atomic.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db_path))

    result = server.checklist_create("Atomic", json.dumps(["valid", ""]))

    assert result.startswith("ERROR: checklist item titles cannot be empty")
    conn = memory_store.connect(str(db_path))
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()


def test_workbench_agent_forces_validation_after_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "agent.db"))
    decisions = [
        '{"tool":"directory_tree","args":{"path":"."}}',
        '{"tool":"file_write","args":{"path":"demo.py","content":"print(1)"}}',
        '{"final":"done too early"}',
        '{"tool":"script_run","args":{"path":"demo.py"}}',
        '{"final":"created and validated demo.py"}',
    ]
    prompts = []

    def fake_generate(prompt, history=None):
        prompts.append(prompt)
        return decisions.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: fake_generate)
    monkeypatch.setattr(
        server,
        "_agent_dispatch_observed",
        lambda tool, args, allow_web=True, read_only=False: (
            "directory tree: workspace"
            if tool == "directory_tree"
            else "file write\n  action: created"
            if tool == "file_write"
            else "script run\n  ok: True\n  returncode: 0"
        ),
    )

    result = server._agent_impl(
        "create and run demo.py",
        max_steps=5,
        auto_checklist=True,
        project="trilobite",
    )

    assert result == "created and validated demo.py"
    assert "HOST REQUIREMENT" in prompts[3]
    conn = memory_store.connect(server._DB_PATH)
    try:
        statuses = [
            row[0]
            for row in conn.execute(
                "SELECT status FROM tasks WHERE parent_id <> '' ORDER BY rowid"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert statuses == ["done", "done", "done", "done"]


def test_nested_agent_report_uses_current_response_not_stale_latest(monkeypatch):
    activity_tracker.reset_for_tests()
    monkeypatch.setattr(server, "_agent_impl", lambda *a, **k: "nested result")

    with activity_tracker.response_span("http", "do work"):
        output = server.agent("do work", checklist=False)

    assert "nested result" in output
    assert "result: complete" in output
    assert "response: r000" in output
    assert "unavailable" not in output


def test_loop_dispatch_supports_workbench_actions(monkeypatch):
    monkeypatch.setattr(server, "directory_tree", lambda **kwargs: "tree ok")
    monkeypatch.setattr(server, "workspace_inventory", lambda **kwargs: "inventory ok")

    result = server._loop_dispatch({"type": "directory_tree", "path": "."})
    inventory = server._loop_dispatch({"type": "workspace_inventory", "path": "."})

    assert result["ok"] is True
    assert result["type"] == "directory_tree"
    assert result["output"] == "tree ok"
    assert inventory["ok"] is True
    assert inventory["output"] == "inventory ok"


def test_loop_dispatch_supports_weather_and_consent_gated_location(monkeypatch):
    monkeypatch.setattr(
        server, "weather_lookup",
        lambda location, forecast_days=3, units="auto": (
            f"weather:{location}:{forecast_days}:{units}"
        ),
    )
    monkeypatch.setattr(
        server, "approximate_location_lookup",
        lambda consent=False: "location:allowed" if consent else "ERROR: consent",
    )

    weather = server._loop_dispatch({
        "type": "weather_lookup", "location": "Chicago", "forecast_days": 2,
    })
    denied = server._loop_dispatch({"type": "approximate_location_lookup"})
    allowed = server._loop_dispatch({
        "type": "approximate_location_lookup", "consent": True,
    })

    assert weather["ok"] is True
    assert weather["output"] == "weather:Chicago:2:auto"
    assert denied["ok"] is False
    assert allowed["output"] == "location:allowed"


def test_loop_dispatch_supports_fleet_capacity_and_cancellation(monkeypatch):
    monkeypatch.setattr(
        server, "master_capacity", lambda requested_agents=0: f"capacity:{requested_agents}",
    )
    monkeypatch.setattr(
        server, "master_cancel", lambda agent_id: f"cancel:{agent_id}",
    )
    monkeypatch.setattr(
        server, "master_retry", lambda agent_id, tier="": f"retry:{agent_id}:{tier}",
    )

    capacity = server._loop_dispatch({
        "type": "master_capacity", "requested_agents": 20,
    })
    cancelled = server._loop_dispatch({
        "type": "master_cancel", "agent_id": "all",
    })
    retried = server._loop_dispatch({
        "type": "master_retry", "agent_id": "master-old", "tier": "code",
    })

    assert capacity["ok"] is True
    assert capacity["output"] == "capacity:20"
    assert cancelled["ok"] is True
    assert cancelled["output"] == "cancel:all"
    assert retried["ok"] is True
    assert retried["output"] == "retry:master-old:code"


def test_validation_must_cover_persistent_mutation_path():
    mutations = [{
        "tool": "file_write",
        "path": server._agent_normalized_path("artifacts/generated/demo.py"),
    }]

    assert server._agent_validation_covers(
        "run_code", {"code": "print('same draft')"}, mutations, "ok"
    ) is False
    assert server._agent_validation_covers(
        "script_run", {"path": "artifacts/generated/demo.py"}, mutations, "ok"
    ) is True
    assert server._agent_validation_covers(
        "workspace_run", {"program": "echo", "args": ["build"]}, mutations, "ok"
    ) is False
    assert server._agent_validation_covers(
        "workspace_run",
        {"program": "python", "args": ["artifacts/generated/demo.py"]},
        mutations,
        "ok",
    ) is True


def test_agent_observation_records_nested_run_code_once():
    activity_tracker.reset_for_tests()

    with activity_tracker.response_span("agent", "run one check"):
        result = server._agent_dispatch_observed(
            "run_code", {"code": "print('ONCE')", "language": "python"}
        )

    latest = activity_tracker.latest()
    actions = [
        event for event in latest["events"] if event.get("kind") == "tool_call"
    ]
    assert "ONCE" in result
    assert len(actions) == 1
    assert actions[0]["tool"] == "run_code"
