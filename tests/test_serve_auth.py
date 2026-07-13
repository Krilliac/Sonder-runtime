from contextlib import contextmanager
import http.client
import json
import os
import threading

import pytest

import sonder_serve as ts
import sonder_health


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


@pytest.mark.parametrize(("messages", "message"), [
    ([None], "messages[0] must be an object"),
    (["junk"], "messages[0] must be an object"),
    ({"role": "user", "content": "hello"}, "messages must be an array"),
    (
        [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
        "messages[0].content must be a string",
    ),
    ([{"content": "hello"}], "messages[0].role is required"),
    ([{"role": "user"}], "messages[0].content is required"),
    (
        [{"role": "tool", "content": "tool output"}],
        "messages[0].role must be one of",
    ),
    ([{"role": "system", "content": "system only"}], "non-empty user message"),
    ([{"role": "user", "content": "   "}], "non-empty user message"),
])
def test_chat_rejects_invalid_messages_with_structured_400(
    monkeypatch, messages, message,
):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    request = json.dumps({"model": "sonder", "messages": messages}).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={"Content-Type": "application/json"},
        )

    payload = json.loads(body)
    assert status == 400
    assert payload["error"]["type"] == "invalid_request"
    assert message in payload["error"]["message"]


def test_chat_accepts_valid_text_messages_and_forwards_history(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    forwarded = []

    def fake_answer(prompt, history, **kwargs):
        forwarded.append((prompt, history))
        return "VALID ANSWER"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "follow up"},
    ]
    request = json.dumps({"model": "sonder", "messages": messages}).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={"Content-Type": "application/json"},
    )

    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"].startswith(
        "VALID ANSWER"
    )
    assert forwarded == [(
        "follow up",
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ],
    )]


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_type", "retry_after"),
    [
        (
            ts.server.ModelCallError(
                "http", "context length exceeded", status=400,
            ),
            400,
            "invalid_request_error",
            None,
        ),
        (
            ts.server.ModelCallError(
                "transport", "connection reset", transient=True,
                attempts=2,
            ),
            503,
            "server_error",
            "1",
        ),
        (
            ts.server.ModelCallError(
                "timeout", "request timed out", transient=True,
                attempts=1,
            ),
            504,
            "server_error",
            "1",
        ),
        (
            ts.server.ModelCallError(
                "http", "upstream request timeout", transient=True,
                status=408, attempts=2,
            ),
            504,
            "server_error",
            "1",
        ),
    ],
)
def test_chat_maps_typed_model_failures_to_http_errors(
    monkeypatch, error, expected_status, expected_type, retry_after,
):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ts.server,
        "answer_with_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={"Content-Type": "application/json"},
        )

    payload = json.loads(body)
    assert status == expected_status
    assert payload["error"] == {
        "message": error.detail,
        "type": expected_type,
    }
    assert headers.get("Retry-After") == retry_after
    assert "choices" not in payload


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


def test_sonder_health_requires_exact_private_loopback_challenge(monkeypatch):
    token = "health-proof-" + ("x" * 32)
    nonce = sonder_health.new_nonce()
    monkeypatch.setattr(ts, "LAUNCHER_HEALTH_TOKEN", token)
    monkeypatch.setattr(ts, "RUNTIME_ROLE", sonder_health.MANAGED_ROLE)

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "GET",
            sonder_health.PATH,
            headers={sonder_health.NONCE_HEADER: nonce},
        )

    assert status == 200
    payload = json.loads(body)
    assert payload == sonder_health.response_payload(
        token, nonce, port, pid=os.getpid()
    )
    assert sonder_health.payload_matches(
        payload, token=token, nonce=nonce, port=port
    )
    assert set(payload) == {
        "identity", "service", "version", "role", "pid", "port", "nonce", "proof",
    }
    assert token not in body.decode("utf-8")


@pytest.mark.parametrize(
    ("configured", "nonce"),
    [
        ("", ""),
        ("x" * 31, "0" * 64),
        ("x" * 32, ""),
        ("x" * 32, "not-a-valid-nonce"),
        ("x" * 32, "A" * 64),
    ],
)
def test_sonder_health_failure_is_indistinguishable(
    monkeypatch,
    configured,
    nonce,
):
    monkeypatch.setattr(ts, "LAUNCHER_HEALTH_TOKEN", configured)
    headers = (
        {sonder_health.NONCE_HEADER: nonce}
        if nonce
        else {}
    )

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "GET", sonder_health.PATH, headers=headers,
        )

    assert status == 404
    assert json.loads(body) == {
        "error": {"message": "not found", "type": "not_found"}
    }


def test_sonder_health_rejects_non_loopback_client(monkeypatch):
    token = "x" * 32
    nonce = sonder_health.new_nonce()
    monkeypatch.setattr(ts, "LAUNCHER_HEALTH_TOKEN", token)
    monkeypatch.setattr(ts, "_is_loopback_host", lambda host: False)

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "GET",
            sonder_health.PATH,
            headers={sonder_health.NONCE_HEADER: nonce},
        )

    assert status == 404
    assert "identity" not in body.decode("utf-8")


def test_sonder_health_rejects_legacy_and_main_api_credentials(monkeypatch):
    token = "x" * 32
    monkeypatch.setattr(ts, "LAUNCHER_HEALTH_TOKEN", token)

    with _http_server(monkeypatch) as port:
        for headers in (
            {"Authorization": "Bearer " + token},
            {"X-Sonder-Launcher-Health-Token": token},
        ):
            status, _, body = _request(
                port,
                "GET",
                sonder_health.PATH,
                headers=headers,
            )
            assert status == 404
            assert "identity" not in body.decode("utf-8")


def test_sonder_health_nonce_is_header_only(monkeypatch):
    token = "x" * 32
    nonce = sonder_health.new_nonce()
    monkeypatch.setattr(ts, "LAUNCHER_HEALTH_TOKEN", token)

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "GET",
            sonder_health.PATH + "?nonce=" + nonce,
        )

    assert status == 404
    assert "proof" not in body.decode("utf-8")
    assert sonder_health.request_path_matches(sonder_health.PATH)
    assert sonder_health.request_path_matches(sonder_health.PATH + "/")
    assert not sonder_health.request_path_matches(sonder_health.PATH + "//")


def test_sonder_health_payload_validator_rejects_tampering_or_replay():
    token = "x" * 32
    nonce = "0" * 64
    payload = sonder_health.response_payload(token, nonce, 11435, pid=123)

    def matches(candidate, **overrides):
        return sonder_health.payload_matches(
            candidate,
            token=overrides.get("token", token),
            nonce=overrides.get("nonce", nonce),
            port=overrides.get("port", 11435),
        )

    assert matches(payload)
    assert not matches({**payload, "extra": True})
    assert not matches({**payload, "service": "other"})
    assert not matches({**payload, "pid": 124})
    assert not matches({**payload, "proof": "f" * 64})
    assert not matches(payload, port=11436)
    assert not matches(payload, nonce="1" * 64)
    assert not matches(payload, token="y" * 32)


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


def test_chat_rejects_non_object_location_hint(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    request = json.dumps({
        "messages": [{"role": "user", "content": "weather in my area"}],
        "location_consent": True,
        "location_hint": "not-an-object",
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 400
    assert json.loads(body)["error"]["message"] == "location_hint must be an object"


def test_chat_forwards_consent_and_client_location_to_web_router(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    calls = []

    def fake_web(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "GROUNDED WEATHER"

    monkeypatch.setattr(ts.server, "chat_web_response", fake_web)
    monkeypatch.setattr(
        ts.server, "answer_with_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model called")),
    )
    hint = {
        "city": "Chicago",
        "region": "Illinois",
        "country": "United States",
        "timezone": "America/Chicago",
    }
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": "weather in my area"}],
        "location_consent": True,
        "location_hint": hint,
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    content = json.loads(body)["choices"][0]["message"]["content"]
    assert "GROUNDED WEATHER" in content
    assert calls[0][0] == "weather in my area"
    assert calls[0][1]["location_consent"] is True
    assert calls[0][1]["location_hint"] == hint
    assert calls[0][1]["allow_server_location_lookup"] is True


def test_dangerous_slash_denied_before_handler(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(
        ts, "_auth_account", lambda header: {"username": "u", "role": "user"}
    )
    called = []
    monkeypatch.setattr(ts, "_handle_slash", lambda *args, **kwargs: called.append(True))
    request = json.dumps({
        "model": "sonder",
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
    [
        "/asset kit logo and sound",
        "/forge suite",
        "/game python 2d demo | platformer",
        "/gamefleet demos | varied games",
    ],
)
def test_artifact_and_game_commands_require_developer_access(prompt):
    assert ts._dangerous_http_slash(prompt) is True


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
        "model": "sonder",
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
        "model": "sonder",
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
