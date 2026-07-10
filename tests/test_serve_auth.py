from contextlib import contextmanager
import http.client
import json
import threading

import pytest

import trilobite_serve as ts


def test_check_auth_open_when_no_key():
    assert ts.check_auth("", "") is True


def test_check_auth_bearer_match():
    assert ts.check_auth("Bearer s3cret", "s3cret") is True


def test_check_auth_raw_match():
    assert ts.check_auth("s3cret", "s3cret") is True


def test_check_auth_wrong_key():
    assert ts.check_auth("Bearer wrong", "s3cret") is False


def test_check_auth_missing_header_when_key_set():
    assert ts.check_auth("", "s3cret") is False


def test_authorized_requires_account_when_flag_set(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account", lambda header: None)

    assert ts._authorized("") is False


def test_authorized_accepts_account_when_flag_set(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account", lambda header: {"username": "u"})

    assert ts._authorized("Bearer token") is True


@contextmanager
def _http_server(monkeypatch):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    payload = response.read()
    result = response.status, dict(response.getheaders()), payload
    conn.close()
    return result


def test_api_key_mode_cannot_be_bypassed_by_account_token(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "k" * 32)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "_auth_account", lambda header: {"role": "admin"})
    assert ts._authorized("Bearer account-token") is False
    assert ts._authorized("Bearer " + ("k" * 32)) is True


def test_non_loopback_bind_fails_without_strong_auth():
    with pytest.raises(RuntimeError):
        ts._validate_bind_security(
            "0.0.0.0", api_key="", auth_mode="local-open", auth_secret=""
        )
    ts._validate_bind_security(
        "0.0.0.0", api_key="k" * 32, auth_mode="api-key", auth_secret=""
    )
    ts._validate_bind_security(
        "127.0.0.1", api_key="", auth_mode="local-open", auth_secret=""
    )


def test_cors_denies_hostile_origin_and_echoes_only_allowlisted(monkeypatch):
    monkeypatch.setattr(ts, "CORS_ORIGINS", frozenset({"https://allowed.example"}))
    with _http_server(monkeypatch) as port:
        status, headers, _ = _request(
            port, "OPTIONS", "/v1/chat/completions",
            headers={"Origin": "https://hostile.example"},
        )
        assert status == 403
        assert "Access-Control-Allow-Origin" not in headers
        status, headers, _ = _request(
            port, "OPTIONS", "/v1/chat/completions",
            headers={"Origin": "https://allowed.example"},
        )
        assert status == 204
        assert headers["Access-Control-Allow-Origin"] == "https://allowed.example"
        assert headers["Vary"] == "Origin"
        status, _, _ = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=b"{}",
            headers={
                "Origin": "https://hostile.example",
                "Content-Type": "application/json",
            },
        )
        assert status == 403


def test_post_body_limit_and_content_type_return_real_4xx(monkeypatch):
    monkeypatch.setattr(ts, "MAX_REQUEST_BYTES", 4)
    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "POST",
            "/missing",
            body=b'{"123":4}',
            headers={"Content-Type": "application/json"},
        )
        assert status == 413
        assert json.loads(body)["error"]["type"] == "invalid_request"
        status, _, body = _request(
            port,
            "POST",
            "/missing",
            body=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        assert status == 415
        assert "Traceback" not in body.decode("utf-8")


def test_dangerous_slash_denied_before_handler(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(
        ts, "_auth_account", lambda header: {"username": "u", "role": "user"}
    )
    called = []
    monkeypatch.setattr(ts, "_handle_slash", lambda *args, **kwargs: called.append(True))
    request = json.dumps({
        "model": "trilobite",
        "messages": [{"role": "user", "content": "/run"}],
    }).encode("utf-8")
    with _http_server(monkeypatch) as port:
        status, _, _ = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={
                "Authorization": "Bearer account-token",
                "Content-Type": "application/json",
            },
        )
    assert status == 403
    assert called == []


@pytest.mark.parametrize(
    "prompt",
    ["run it", "train yourself", "trace on", "strict on"],
)
def test_ordinary_account_cannot_trigger_natural_control_intents(
    monkeypatch, prompt
):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(
        ts,
        "_auth_account",
        lambda header: {"username": "u", "role": "user"},
    )
    intent_calls = []
    monkeypatch.setattr(
        ts,
        "_handle_intent",
        lambda *args, **kwargs: intent_calls.append((args, kwargs)) or "CONTROL",
    )

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(
        ts.admin_auth, "rate_limit", lambda conn, account: (True, "")
    )
    monkeypatch.setattr(
        ts.server,
        "answer_with_history",
        lambda *args, **kwargs: "model answer\n\n[interaction_id: abc123]",
    )
    request = json.dumps({
        "model": "trilobite",
        "session": "ordinary-user-chat",
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={
                "Authorization": "Bearer account-token",
                "Content-Type": "application/json",
            },
        )

    assert status == 200
    assert intent_calls == []
    assert "model answer" in json.loads(body)["choices"][0]["message"]["content"]


def test_durable_session_and_project_ids_are_principal_scoped(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")

    def account_for(header):
        token = ts._bearer_token(header)
        return {"username": token.split("-", 1)[0], "role": "user"}

    monkeypatch.setattr(ts, "_auth_account", account_for)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(
        ts.admin_auth, "rate_limit", lambda conn, account: (True, "")
    )
    forwarded = []

    def fake_answer(prompt, history, **kwargs):
        forwarded.append((kwargs["session"], kwargs["project"]))
        return "answer\n\n[interaction_id: abc123]"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)
    request = json.dumps({
        "model": "trilobite",
        "session": "common-session",
        "project": "common-project",
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        for token in ("alice-token", "bob-token", "alice-token"):
            status, _, _ = _request(
                port,
                "POST",
                "/v1/chat/completions",
                body=request,
                headers={
                    "Authorization": "Bearer " + token,
                    "Content-Type": "application/json",
                },
            )
            assert status == 200

    assert forwarded[0] == forwarded[2]
    assert forwarded[0] != forwarded[1]
    assert all(session != "common-session" for session, _ in forwarded)
    assert all(project != "common-project" for _, project in forwarded)
