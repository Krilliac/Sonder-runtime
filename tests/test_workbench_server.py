import json

import activity_tracker
import memory_store
import server


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

    result = server._loop_dispatch({"type": "directory_tree", "path": "."})

    assert result["ok"] is True
    assert result["type"] == "directory_tree"
    assert result["output"] == "tree ok"


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
