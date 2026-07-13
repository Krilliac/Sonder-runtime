import json

import memory_store
import server


def test_memory_search_finds_lessons_facts_sessions_and_interactions(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.add_lesson(conn, "L1", "prefer deque for queue operations", None, "i1")
        memory_store.add_fact(conn, "F1", "proj", "this project uses deque queues", None)
        memory_store.touch_session(conn, "S1", "proj")
        memory_store.set_session_title(conn, "S1", "deque debugging")
        memory_store.log_interaction(conn, "I1", "fix deque bug", "", "use popleft", "code", session_id="S1")
    finally:
        conn.close()
    out = server.memory_search("deque", limit=5)
    assert "L1" in out
    assert "F1" in out
    assert "S1" in out
    assert "I1" in out


def test_memory_export_is_json(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.add_lesson(conn, "L1", "lesson text", None, "i1")
    finally:
        conn.close()
    data = json.loads(server.memory_export(limit=5))
    assert data["lessons"][0]["id"] == "L1"
    assert "outcomes" in data


def test_session_export_by_id(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.touch_session(conn, "S1", "proj")
        memory_store.set_session_title(conn, "S1", "demo")
        memory_store.log_interaction(conn, "I1", "hello", "", "hi back", "code", session_id="S1")
    finally:
        conn.close()
    out = server.session_export("S1")
    assert "title: demo" in out
    assert "USER: hello" in out
    assert "ASSISTANT: hi back" in out


def test_tool_manifest_mentions_workflows():
    out = server.tool_manifest()
    assert "workflow" in out
    assert "memory_search" in out
    assert "memory_interaction_embedding_backfill" in out
