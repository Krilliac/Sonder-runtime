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
