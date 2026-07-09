import server


def test_task_tools_round_trip(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db))

    created = server.task_create("wire visible todos", priority=1)
    assert "task created" in created
    listed = server.task_list()
    assert "wire visible todos" in listed

    task_id = listed.splitlines()[1].split()[0]
    updated = server.task_update(task_id, status="done")
    assert "done" in updated
    shown = server.task_show(task_id)
    assert "events:" in shown


def test_command_and_compaction_tools(monkeypatch, tmp_path):
    db = tmp_path / "memory.db"
    monkeypatch.setattr(server, "_DB_PATH", str(db))

    assert "/todo" in server.command_registry_list("planning")
    plan = server.context_compaction_plan()
    assert "trilobite context compaction plan" in plan
    assert "recommended actions" in plan


def test_permission_policy_tool(monkeypatch, tmp_path):
    monkeypatch.setattr(server.trilobite_paths, "default_home", lambda: tmp_path)
    out = server.permission_policy("file_delete")
    assert "permission check: file_delete" in out
    assert "action: deny" in out
