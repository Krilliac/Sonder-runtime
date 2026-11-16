"""``GET /v1/sessions/<id>/transcript``: the full chat from the session event log."""
from __future__ import annotations

import json

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.interfaces.http.facades.session import dispatch_session_route
from tests.test_serve_auth import _http_server, _request

pytestmark = pytest.mark.unit

SID = "chat-1794840126388000"
OTHER = "chat-1794840126388999"


def _turn(repo, session, n, *, tier="fast", answered="qwen-7b", fail=None, text=None):
    """One captured turn shaped like ``SessionCapture`` writes it."""
    turn, req = "turn-%d" % n, "req-%d" % n
    repo.append(session, "model.requested", {
        "request_id": req, "turn_id": turn, "prompt": "p%d" % n, "tier": tier, "history": []})
    repo.append(session, "user.message", {
        "content": text or "question %d" % n, "turn_id": turn, "request_id": req})
    if fail:
        repo.append(session, "model.failed", {
            "turn_id": turn, "request_id": req, "error_code": fail})
        return
    repo.append(session, "provider.requested", {
        "turn_id": turn, "request_id": req, "attempt_id": "a%d" % n,
        "provider": "ollama", "operation": "chat", "payload": {}})
    repo.append(session, "provider.responded", {
        "turn_id": turn, "request_id": req, "attempt_id": "a%d" % n,
        "response": {"model": answered}})
    repo.append(session, "model.response", {
        "content": "answer %d" % n, "turn_id": turn, "request_id": req})


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    for n in range(1, 16):  # 15 turns: the live-history export showed only 4
        _turn(repository, SID, n, tier="deep" if n % 2 else "fast",
              answered="big-model" if n % 2 else "small-model")
    _turn(repository, SID, 16, fail="DEADLINE_EXCEEDED")
    _turn(repository, OTHER, 1, text="other account secret chat")
    return repository


@pytest.fixture
def facade(repo, monkeypatch):
    value = HttpSessionFacade(repo, max_page_size=10, max_scan=50)
    monkeypatch.setattr(ts, "_SESSION_FACADE", value)
    return value


def test_json_transcript_has_every_turn_in_sequence_order(facade):
    result = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID,
                                    query={"format": ["json"]})
    assert result.status_code == 200
    body = result.body
    assert body["schema"] == "sonder.http-session-transcript.v1"
    assert body["format"] == "json" and body["truncated"] is False
    messages = body["messages"]
    users = [m for m in messages if m["role"] == "user"]
    answers = [m for m in messages if m["role"] == "assistant"]
    assert len(users) == 16 and len(answers) == 15
    assert [m["sequence"] for m in messages] == sorted(m["sequence"] for m in messages)
    assert [m["content"] for m in users][:3] == ["question 1", "question 2", "question 3"]
    # Per-turn tier and answering model.
    assert (answers[0]["tier"], answers[0]["model"]) == ("deep", "big-model")
    assert (answers[1]["tier"], answers[1]["model"]) == ("fast", "small-model")
    assert (users[2]["tier"]) == "deep"
    failure = [m for m in messages if m["role"] == "failure"]
    assert len(failure) == 1
    assert failure[0]["error_code"] == "DEADLINE_EXCEEDED" and failure[0]["turn_id"] == "turn-16"
    assert failure[0]["sequence"] > users[-1]["sequence"] - 1
    assert all("other account" not in m["content"] for m in messages)


def test_markdown_is_the_default_and_names_tier_model_and_failure(facade):
    result = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID)
    assert result.status_code == 200 and result.body["format"] == "md"
    text = result.body["markdown"]
    assert text.startswith("# Chat transcript: " + SID)
    assert "question 15" in text and "answer 15" in text
    assert "tier=deep" in text and "model=big-model" in text
    assert "FAILED (DEADLINE_EXCEEDED)" in text
    assert text.index("question 1\n") < text.index("question 2\n") < text.index("question 15")


def test_secrets_are_redacted(repo, facade):
    _turn(repo, "chat-secret", 1, text="my key is sk-proj-AbCdEfGhIjKlMnOpQrStUvWx")
    result = dispatch_session_route(facade, "/v1/sessions/chat-secret/transcript")
    assert "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx" not in result.body["markdown"]


def test_unknown_bad_id_and_bad_format(facade):
    assert dispatch_session_route(facade, "/v1/sessions/chat-missing/transcript").status_code == 404
    assert dispatch_session_route(facade, "/v1/sessions/%s/transcript" % "a:b").status_code == 400
    assert dispatch_session_route(facade, "/v1/sessions/../transcript").status_code in (400, 404)
    bad = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID,
                                 query={"format": ["xml"]})
    assert bad.status_code == 400


def test_output_is_capped_with_a_truncation_note(repo):
    capped = HttpSessionFacade(repo, max_page_size=10, max_scan=50, transcript_max_bytes=4096)
    md = dispatch_session_route(capped, "/v1/sessions/%s/transcript" % SID).body
    assert md["truncated"] is True and "transcript truncated" in md["markdown"]
    assert len(md["markdown"].encode("utf-8")) <= 4096 + 512
    js = dispatch_session_route(capped, "/v1/sessions/%s/transcript" % SID,
                                query={"format": ["json"]}).body
    assert js["truncated"] is True and 0 < js["message_count"] < 32


def test_one_huge_message_is_clipped_not_dropped(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "big.db", max_read_limit=100)
    _turn(repository, SID, 1, text="x" * 20_000)
    small = HttpSessionFacade(repository, transcript_max_bytes=2048)
    body = dispatch_session_route(small, "/v1/sessions/%s/transcript" % SID,
                                  query={"format": ["json"]}).body
    assert body["truncated"] is True
    assert 0 < len(body["messages"][0]["content"]) < 2048


# --- authorization parity with the other session read routes -----------------

MODES = [
    ("local-open", None, False, True), ("api-key", None, True, True),
    ("account", "user", False, False), ("account", "developer", False, False),
    ("account", "admin", False, True), ("both", "user", True, False),
    ("both", "admin", True, True),
]


@pytest.mark.parametrize("mode,role,key,allowed", MODES)
def test_transcript_auth_matches_export_and_events(monkeypatch, facade, mode, role, key, allowed):
    context = {"mode": mode, "authorized": True, "api_key": key,
               "account": {"username": "caller", "role": role} if role else None}
    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda self: context)
    seen = {}
    with _http_server(monkeypatch) as port:
        for route, query in (("transcript", ""), ("export", "?max_events=10"),
                             ("events", "?page_size=5")):
            status, _, body = _request(port, "GET", "/v1/sessions/%s/%s%s" % (SID, route, query))
            seen[route] = status
            if not allowed:
                assert "question" not in body.decode("utf-8", "replace")
        if allowed:
            status, _, body = _request(port, "GET", "/v1/sessions/%s/transcript?format=json" % SID)
            assert json.loads(body)["message_count"] == 32
    expected = 200 if allowed else 403
    assert seen == {"transcript": expected, "export": expected, "events": expected}


def test_unauthenticated_request_is_refused(monkeypatch, facade):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    monkeypatch.setattr(ts, "_auth_account", lambda header: None)
    with _http_server(monkeypatch) as port:
        for route in ("transcript", "export"):
            assert _request(port, "GET", "/v1/sessions/%s/%s" % (SID, route))[0] == 401
