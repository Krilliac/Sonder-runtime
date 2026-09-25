"""App-facing HTTP contract additions (app plan lane S).

S3 client-neutral pending work text, S4 the registration 201 ``message``,
S5 ``history: "client"``, and S7 ``GET /v1/sessions``.
"""
from __future__ import annotations

import json

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.chat.handoff_receipts import ChatWorkResult
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from tests.test_serve_auth import _http_server


def _request(port, method, path, body=None, headers=None):
    """Like test_serve_auth._request, with room for a cold catalog on a busy host."""
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()

pytestmark = pytest.mark.unit

JSON = {"Content-Type": "application/json"}


def _local_open(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)


# --- S3 ---------------------------------------------------------------------

def test_pending_work_text_names_no_routes_and_the_receipt_carries_them():
    text = ts._work_run_pending_text("wr-" + "a" * 32)
    assert "wr-" + "a" * 32 in text
    assert "/v1/" not in text and "GET " not in text and "POST " not in text
    receipt = ChatWorkResult(text, "running", work_run_id="wr-abc").public_receipt()
    assert receipt["get_url"] == "/v1/work-runs/wr-abc"
    assert receipt["cancel_url"] == "/v1/work-runs/wr-abc/cancel"
    assert "get_url" not in ChatWorkResult("done", "returned").public_receipt()


# --- S4 ---------------------------------------------------------------------

def test_register_201_carries_a_message(monkeypatch, tmp_path):
    import memory_store

    path = str(tmp_path / "register.sqlite")
    monkeypatch.setenv("SONDER_BOOTSTRAP_SECRET", "bootstrap-secret-123456")
    monkeypatch.setattr(ts.server, "_open_db", lambda: memory_store.connect(path))
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "ALLOW_REGISTRATION", False)
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    body = json.dumps({"username": "owner", "password": "password123"})
    with _http_server(monkeypatch) as port:
        status, _, payload = _request(
            port, "POST", "/v1/sonder/register", body=body,
            headers={**JSON, "X-Sonder-Bootstrap-Secret": "bootstrap-secret-123456"})
    assert status == 201, payload
    reply = json.loads(payload)
    assert reply["ok"] is True
    assert reply["message"] == "Account owner created (role admin)."


def test_account_created_message_tolerates_partial_accounts():
    assert ts._account_created_message({"username": "bob"}) == "Account bob created (role user)."
    assert ts._account_created_message(None) == "Account (unnamed) created (role user)."


# --- S5 ---------------------------------------------------------------------

@pytest.fixture
def chat_history(monkeypatch):
    _local_open(monkeypatch)
    seen = {"server_history": 0, "history": None}

    def server_side(storage_session, limit=ts.SERVER_SIDE_HISTORY_TURNS):
        seen["server_history"] += 1
        return [{"role": "user", "content": "a cancelled first turn"},
                {"role": "assistant", "content": "partial"}]

    def answer(prompt, history=None, *args, **kwargs):
        seen["history"] = list(history or [])
        return "answer"

    monkeypatch.setattr(ts, "_server_side_history", server_side)
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *a, **k: None)
    monkeypatch.setattr(ts.server, "answer_with_history", answer)
    return seen


def _chat(port, **extra):
    body = {"model": "sonder", "session": "thread-1",
            "messages": [{"role": "user", "content": "hello"}], **extra}
    status, _, payload = _request(port, "POST", "/v1/chat/completions",
                                  body=json.dumps(body), headers=JSON)
    return status, json.loads(payload)


def test_history_client_never_injects_durable_history(monkeypatch, chat_history):
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, history="client")
    assert status == 200, reply
    assert chat_history["server_history"] == 0
    assert not any("cancelled" in m["content"] for m in chat_history["history"] or [])


def test_history_default_still_continues_a_thin_client_session(monkeypatch, chat_history):
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port)
        assert status == 200, reply
        status, reply = _chat(port, history="auto")
        assert status == 200, reply
    assert chat_history["server_history"] == 2


@pytest.mark.parametrize("value", ["server", "", 1, True, ["client"]])
def test_history_rejects_unknown_values(monkeypatch, chat_history, value):
    with _http_server(monkeypatch) as port:
        status, reply = _chat(port, history=value)
    assert status == 400
    assert "history" in reply["error"]["message"]


# --- S7 ---------------------------------------------------------------------

@pytest.fixture
def sessions(tmp_path, monkeypatch):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append("s-old", "user.message", {"content": "Why does the PSO compile stall?"},
                occurred_at_utc="2026-09-20T10:00:00Z")
    repo.append("s-old", "model.response", {"content": "Because ..."},
                occurred_at_utc="2026-09-20T10:01:00Z")
    repo.append("s-new", "user.message",
                {"content": "  Asset import bug\nwith api_key=sk-secretsecretsecret  " + "x" * 200},
                occurred_at_utc="2026-09-25T09:00:00Z")
    repo.append("s-new", "user.message", {"content": "second turn"},
                occurred_at_utc="2026-09-25T09:05:00Z")
    repo.append("s-mid", "model.response", {"content": "no user turn"},
                occurred_at_utc="2026-09-22T09:00:00Z")
    facade = HttpSessionFacade(repo)
    monkeypatch.setattr(ts, "_SESSION_FACADE", facade)
    return facade


def test_session_list_pages_newest_first_with_redacted_titles(sessions):
    first = sessions.list_sessions(limit=2)
    assert first.status_code == 200
    rows = first.body["sessions"]
    assert [row["id"] for row in rows] == ["s-new", "s-mid"]
    assert rows[0]["turns"] == 2 and rows[0]["events"] == 2
    assert rows[0]["updated"] == "2026-09-25T09:05:00Z"
    assert rows[0]["title"].startswith("Asset import bug with api_key=")
    assert "sk-secret" not in rows[0]["title"]
    assert len(rows[0]["title"]) <= HttpSessionFacade.TITLE_CHARS
    assert rows[1]["title"] == "" and rows[1]["turns"] == 0
    second = sessions.list_sessions(limit=2, after=first.body["next_cursor"])
    assert [row["id"] for row in second.body["sessions"]] == ["s-old"]
    assert second.body["sessions"][0]["title"] == "Why does the PSO compile stall?"
    assert second.body["next_cursor"] is None
    assert sessions.list_sessions(limit=0).status_code == 400
    assert sessions.list_sessions(after="!!").status_code == 400


def test_session_list_route_is_admin_only(monkeypatch, sessions):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    accounts = {"u": {"username": "u", "role": "user"}, "a": {"username": "a", "role": "admin"}}
    monkeypatch.setattr(ts, "_auth_account",
                        lambda header: accounts.get(str(header or "").replace("Bearer ", "")))
    with _http_server(monkeypatch) as port:
        assert _request(port, "GET", "/v1/sessions")[0] == 401
        assert _request(port, "GET", "/v1/sessions", headers={"Authorization": "Bearer u"})[0] == 403
        status, _, payload = _request(port, "GET", "/v1/sessions?limit=1",
                                      headers={"Authorization": "Bearer a"})
        assert status == 200, payload
        body = json.loads(payload)
        assert body["schema"] == "sonder.http-session-list.v1"
        assert [row["id"] for row in body["sessions"]] == ["s-new"]
        status, _, _ = _request(port, "GET", "/v1/sessions?limit=1&bogus=1",
                                headers={"Authorization": "Bearer a"})
        assert status == 400
        # The per-session routes still resolve under the same prefix.
        status, _, _ = _request(port, "GET", "/v1/sessions/s-old/events",
                                headers={"Authorization": "Bearer a"})
        assert status == 200
