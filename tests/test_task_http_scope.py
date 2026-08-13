"""Account boundaries for the HTTP task/checklist surface."""
import re

import server
import sonder_serve as serve


def _account(name):
    return {"account": {"username": name, "role": "user"}}


def _task_id(text):
    match = re.search(r"\b([0-9a-f]{8})\b", text)
    assert match, text
    return match.group(1)


def test_served_todos_are_account_scoped_but_local_mcp_stays_global(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice = _account("alice")
    bob = _account("bob")
    created = serve._handle_slash("/todo add Alice private task", context=alice)
    task_id = _task_id(created)

    assert "Alice private task" in serve._handle_slash("/todo list", context=alice)
    assert "no matching tasks" in serve._handle_slash("/todo list", context=bob)
    assert "no task" in serve._handle_slash("/todo show %s" % task_id, context=bob)
    # Local-open preserves the legacy global operator view, including scoped
    # rows, while account-backed callers remain fail-closed to other scopes.
    assert "Alice private task" in serve._handle_slash("/todo list")

    assert "task created" in server.task_create("local global task")
    assert "local global task" in server.task_list()


def test_catalogued_task_and_checklist_tools_cannot_cross_account_or_leak_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)

    alice = _account("alice")
    bob = _account("bob")
    task = serve._dispatch_catalogued_tool("/task_create title=private", serve._LEGACY_STATE, alice)
    task_id = _task_id(task)
    assert "private" in serve._dispatch_catalogued_tool("/task_list", serve._LEGACY_STATE, alice)
    assert "no matching tasks" in serve._dispatch_catalogued_tool("/task_list", serve._LEGACY_STATE, bob)
    assert "no unique task" in serve._dispatch_catalogued_tool(
        "/task_delete task_id=%s" % task_id, serve._LEGACY_STATE, bob,
    )

    scope = serve._task_account_scope(alice)
    checklist = server.scoped_task_tool_dispatch(
        "checklist_create", {"title": "Alice plan", "items_json": '["one"]'},
        account_scope=scope,
    )
    assert "Alice plan" in checklist
    assert "Alice plan" in serve._handle_slash("/checklist", context=alice)
    assert "no checklist yet" in serve._handle_slash("/checklist", context=bob)


def test_task_scope_is_opaque_and_only_accounts_receive_one():
    scope = serve._task_account_scope(_account("alice"))
    assert scope.startswith("ta-")
    assert "alice" not in scope
    assert scope == serve._task_account_scope(_account("alice"))
    assert scope != serve._task_account_scope(_account("bob"))
    assert serve._task_account_scope({}) is None
