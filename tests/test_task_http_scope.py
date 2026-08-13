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


def test_scoped_task_show_events_alias_and_latest_checklist_use_updated_order(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    alice = _account("alice")
    scope = serve._task_account_scope(alice)
    task_id = _task_id(server.scoped_task_tool_dispatch(
        "task_create", {"title": "private"}, account_scope=scope,
    ))
    assert "events:" not in server.scoped_task_tool_dispatch(
        "task_show", {"task_id": task_id, "events": False}, account_scope=scope,
    )

    older = server.scoped_task_tool_dispatch(
        "checklist_create", {"title": "older", "items_json": '["one"]', "priority": 1},
        account_scope=scope,
    )
    newer = server.scoped_task_tool_dispatch(
        "checklist_create", {"title": "newer", "items_json": '["two"]', "priority": 2},
        account_scope=scope,
    )
    assert "older" in older and "newer" in newer
    # Priority sorting puts "older" first in a normal task list; the bare
    # checklist command must instead choose its newest durable parent.
    assert "newer" in serve._handle_slash("/checklist", context=alice)


def test_task_scope_is_opaque_and_only_accounts_receive_one():
    scope = serve._task_account_scope(_account("alice"))
    assert scope.startswith("ta-")
    assert "alice" not in scope
    assert scope == serve._task_account_scope(_account("alice"))
    assert scope != serve._task_account_scope(_account("bob"))
    assert serve._task_account_scope({}) is None


def test_account_http_rejects_indirect_global_task_paths(monkeypatch):
    alice = _account("alice")
    calls = []
    monkeypatch.setattr(server, "workbench_agent", lambda **kwargs: calls.append(kwargs) or "ran")
    monkeypatch.setattr(server, "loop", lambda **kwargs: calls.append(kwargs) or "loop ran")
    monkeypatch.setattr(server, "master_orchestrate", lambda **kwargs: calls.append(kwargs) or "master ran")
    monkeypatch.setattr(server, "master_retry", lambda **kwargs: calls.append(kwargs) or "retry ran")
    monkeypatch.setattr(
        server, "control_command",
        lambda *args, **kwargs: calls.append((args, kwargs)) or "control ran",
    )

    assert "account-scoped task state" in serve._handle_slash("/work inspect this", context=alice)
    assert not calls
    assert "account-scoped task state" in serve._dispatch_catalogued_tool(
        "/workbench_agent prompt=inspect", serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "account-scoped task state" in serve._dispatch_catalogued_tool(
        "/agent prompt=inspect", serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "loop action 'checklist_create'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"type":"checklist_create","title":"x"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "loop action 'workbench_agent'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"type":"workbench_agent","prompt":"inspect"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "account-scoped task state" in serve._handle_slash(
        "/master inline inspect this", context=alice,
    )
    assert not calls
    assert "account-scoped task state" in serve._dispatch_catalogued_tool(
        "/master_retry agent_id=abc", serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "account-scoped task state" in serve._handle_slash(
        "/agentretry abc", context=alice,
    )
    assert not calls
    assert "loop action 'master_retry'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"type":"master_retry","agent_id":"abc"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls
    assert "loop action 'agent_retry'" in serve._dispatch_catalogued_tool(
        '/loop actions_json=[{"action":"agent_retry","agent_id":"abc"}]',
        serve._LEGACY_STATE, alice,
    )
    assert not calls

    # Local-open/direct operator use remains backward-compatible and global.
    assert serve._handle_slash("/work inspect this") == "ran"
    assert serve._handle_slash("/master inline inspect this") == "master ran"
    assert serve._dispatch_catalogued_tool(
        "/master_retry agent_id=abc", serve._LEGACY_STATE,
    ) == "retry ran"
    assert serve._handle_slash("/agentretry abc") == "control ran"
    assert calls
