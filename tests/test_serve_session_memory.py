"""Thin-client server-side session memory fallback (Jetson-run finding)."""
from __future__ import annotations

import pytest

import sonder_runtime.interfaces.http.serve as sonder_serve


def test_client_supplied_history_is_used_verbatim():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply one"},
        {"role": "user", "content": "second"},
    ]
    history = sonder_serve._history_from_messages(messages)
    assert history == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply one"},
    ]


def test_empty_session_yields_no_server_history():
    assert sonder_serve._server_side_history("") == []
    assert sonder_serve._server_side_history(None) == []


def test_server_side_history_rebuilds_from_stored_turns(monkeypatch):
    stored = [
        {"id": "1", "task": "My target is a Jetson Orin Nano.",
         "response": "Acknowledged.\n=== ACTIVITY ===\nnoise"},
        {"id": "2", "task": "Anything else?",
         "response": "No.\n\n--- footer ---"},
    ]
    monkeypatch.setattr(sonder_serve.server, "_resolve_session", lambda s: "sid")

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(sonder_serve.server, "_open_db", lambda: _Conn())
    import memory_store
    monkeypatch.setattr(memory_store, "session_turns", lambda conn, sid: stored)

    history = sonder_serve._server_side_history("demo")
    assert {"role": "user", "content": "My target is a Jetson Orin Nano."} in history
    # Activity footer is stripped from replayed assistant turns.
    assert any(h["role"] == "assistant" and "ACTIVITY" not in h["content"]
               for h in history)
    assert all("=== ACTIVITY" not in h["content"] for h in history)


def test_server_side_history_limit_is_bounded(monkeypatch):
    stored = [
        {"id": str(i), "task": f"turn {i}", "response": f"resp {i}"}
        for i in range(50)
    ]
    monkeypatch.setattr(sonder_serve.server, "_resolve_session", lambda s: "sid")

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(sonder_serve.server, "_open_db", lambda: _Conn())
    import memory_store
    monkeypatch.setattr(memory_store, "session_turns", lambda conn, sid: stored)

    history = sonder_serve._server_side_history("demo", limit=4)
    # 4 turns -> at most 8 messages (user+assistant each).
    assert len(history) <= 8
    assert history[-1]["content"] == "resp 49"


def test_server_side_history_survives_store_errors(monkeypatch):
    monkeypatch.setattr(sonder_serve.server, "_resolve_session",
                        lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sonder_serve._server_side_history("demo") == []


def _durable_facade(tmp_path, session_id="demo"):
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
    from sonder_runtime.application.session.http_facade import HttpSessionFacade

    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    repository.append(session_id, "user.message", {"content": "My codeword is ZEBRA."})
    repository.append(session_id, "model.response", {"content": "Noted."})
    return HttpSessionFacade(repository, max_replay_events=1_000)


def _legacy_turns(monkeypatch, turns):
    monkeypatch.setattr(sonder_serve.server, "_resolve_session", lambda s: "sid")

    class _Conn:
        def close(self):
            pass

    monkeypatch.setattr(sonder_serve.server, "_open_db", lambda: _Conn())
    import memory_store
    monkeypatch.setattr(memory_store, "session_turns", lambda conn, sid: list(turns))


def test_server_side_history_reads_durable_session_capture(monkeypatch, tmp_path):
    """Explicit-model HTTP turns are captured only in the durable session store.

    A pinned model (``"model": "qwen2.5:0.5b"``) is a non-learning route: it
    writes no legacy interaction row, but the HTTP adapter still captures the
    named turn through the typed session capture service.  A thin client that
    names the session must get that transcript back instead of an empty
    history.
    """
    _legacy_turns(monkeypatch, [])
    monkeypatch.setattr(sonder_serve, "_SESSION_FACADE", _durable_facade(tmp_path))

    assert sonder_serve._server_side_history("demo") == [
        {"role": "user", "content": "My codeword is ZEBRA."},
        {"role": "assistant", "content": "Noted."},
    ]


def test_server_side_history_durable_transcript_is_bounded(monkeypatch, tmp_path):
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
    from sonder_runtime.application.session.http_facade import HttpSessionFacade

    repository = SQLiteSessionRepository(tmp_path / "bounded.db", max_read_limit=100)
    for index in range(20):
        repository.append("demo", "user.message", {"content": "turn %d" % index})
        repository.append("demo", "model.response", {"content": "resp %d" % index})
    _legacy_turns(monkeypatch, [])
    monkeypatch.setattr(sonder_serve, "_SESSION_FACADE",
                        HttpSessionFacade(repository, max_replay_events=1_000))

    history = sonder_serve._server_side_history("demo", limit=4)
    assert len(history) == 8
    assert history[0] == {"role": "user", "content": "turn 16"}
    assert history[-1] == {"role": "assistant", "content": "resp 19"}


def test_server_side_history_falls_back_to_legacy_turns(monkeypatch, tmp_path):
    # A session that predates durable capture keeps its legacy history.
    monkeypatch.setattr(sonder_serve, "_SESSION_FACADE", _durable_facade(tmp_path, "other"))
    _legacy_turns(monkeypatch, [
        {"id": "1", "task": "legacy question", "response": "legacy answer"},
    ])

    assert sonder_serve._server_side_history("demo") == [
        {"role": "user", "content": "legacy question"},
        {"role": "assistant", "content": "legacy answer"},
    ]
