"""Cross-surface chat synchronization tests (HTTP API work runs <-> sessions.db <-> REPL)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import server
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.interfaces.http import serve
from sonder_runtime.interfaces.repl import repl


def _wait_work(result, context):
    principal = serve._state_principal(context)
    for _ in range(200):
        record = serve._WORK_RUNNER.get(result.work_run_id, principal)
        if record and record["status"] != "running":
            return record
        threading.Event().wait(0.05)
    raise AssertionError("work run did not finish")


def test_http_routed_work_records_transcript_and_surfaces_in_sessions_list_and_replay(
    tmp_path, monkeypatch,
):
    database = tmp_path / "sync-sessions.sqlite"
    repository = SQLiteSessionRepository(database)
    monkeypatch.setattr(
        bootstrap_app, "default_app",
        lambda: SimpleNamespace(session_repository=lambda: repository),
    )
    monkeypatch.setattr(
        server, "route_work_request",
        lambda prompt, **kwargs: "Built the arena scoreboard cleanly.",
    )
    context = {"mode": "local-open"}
    result = serve._handle_work_intent(
        "Build the arena scoreboard overlay in D:/Sonder Games/FpsGame_Skeleton",
        project="FpsGame_Skeleton",
        authorized=True,
        context=context,
        session_id="arena-sync-test",
        session_ref="arena-sync-test",
        correlation_id="corr-sync-1",
        with_receipt=True,
        record_transcript=True,
    )
    assert result.status == "running"
    _wait_work(result, context)

    # Hash chain integrity must remain completely valid.
    replayed_capture = SessionCaptureService(SQLiteSessionRepository(database)).replay("arena-sync-test")
    assert replayed_capture.crash_safe is True

    facade = HttpSessionFacade(SQLiteSessionRepository(database))
    listed = facade.list_sessions(limit=20)
    assert listed.status_code == 200
    rows = listed.body["sessions"]
    assert len(rows) == 1
    assert rows[0]["id"] == "arena-sync-test"
    assert rows[0]["turns"] == 1
    assert "Build the arena scoreboard" in rows[0]["title"]

    replayed = facade.replay("arena-sync-test")
    assert replayed.status_code == 200
    transcript = replayed.body["transcript"]
    roles = [(m["role"], m["content"]) for m in transcript]
    assert roles == [
        ("user", "Build the arena scoreboard overlay in D:/Sonder Games/FpsGame_Skeleton"),
        ("assistant", "Built the arena scoreboard cleanly."),
    ]


def test_repl_mirrors_work_and_lists_http_sessions(tmp_path, monkeypatch):
    database = tmp_path / "repl-sync.sqlite"
    repository = SQLiteSessionRepository(database)
    monkeypatch.setattr(
        bootstrap_app, "default_app",
        lambda: SimpleNamespace(session_repository=lambda: repository),
    )
    # Seed an HTTP-originated session in sessions.db.
    repository.append("http-chat-42", "user.message", {
        "content": "Continue building the FPS arena game",
        "turn_id": "t-http-1",
    })
    repository.append("http-chat-42", "model.response", {
        "content": "Added the HUD weapon switcher.",
        "turn_id": "t-http-1",
    })

    # Mirror a REPL workspace work turn into sessions.db.
    repl._mirror_repl_turn_to_session_log(
        "repl-work-99",
        "Fix compiler warnings in PlayerHud.cs",
        "Fixed all 14 warnings.",
        lane="workbench",
    )

    # Empty memory.db connection (REPL should still see both sessions via sessions.db).
    import sqlite3
    from sonder_runtime.adapters import memory_store

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    memory_store.init_db(conn)
    try:
        merged = repl._merged_repl_sessions(conn, limit=20)
        ids = {row["session_id"]: row for row in merged}
        assert "http-chat-42" in ids
        assert "repl-work-99" in ids
        assert ids["http-chat-42"]["turn_count"] == 1
        assert "Continue building" in ids["http-chat-42"]["title"]
        assert ids["repl-work-99"]["turn_count"] == 1

        assert repl._find_repl_session(conn, "http-chat-42") == "http-chat-42"
        assert repl._find_repl_session(conn, "http-chat") == "http-chat-42"
        assert repl._find_repl_session(conn, "Continue building") == "http-chat-42"
    finally:
        conn.close()
