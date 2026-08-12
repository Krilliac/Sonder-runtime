from contextlib import contextmanager
import http.client
import json
import os
import socket
import threading

import pytest

import sonder_serve as ts
import sonder_config
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


def test_execution_feed_detail_requires_flag_developer_and_non_local_open(
    monkeypatch,
):
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")
    ordinary = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"role": "user"},
    }
    developer = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"role": "developer"},
    }
    owner_key = {
        "mode": "api-key", "authorized": True, "api_key": True,
        "account": None,
    }
    local_open = {
        "mode": "local-open", "authorized": True, "api_key": False,
        "account": None,
    }

    assert ts._execution_feed_detail_allowed(ordinary) is False
    assert ts._execution_feed_detail_allowed(developer) is True
    assert ts._execution_feed_detail_allowed(owner_key) is True
    assert ts._execution_feed_detail_allowed(local_open) is False
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "true")
    assert ts._execution_feed_detail_allowed(developer) is False


def test_system_operation_roles_separate_authentication_from_authority():
    """An ordinary account never gains global-policy authority by prompting."""
    ordinary = {"mode": "account", "authorized": True, "api_key": False,
                "account": {"username": "ordinary", "role": "user"}}
    developer = {"mode": "account", "authorized": True, "api_key": False,
                 "account": {"username": "dev", "role": "developer"}}
    admin = {"mode": "account", "authorized": True, "api_key": False,
             "account": {"username": "admin", "role": "admin"}}
    assert "administrator" in ts._system_operation_authority_error(
        "permission_mode_change", ordinary,
    )
    assert "developer" in ts._system_operation_authority_error(
        "workspace_execution", ordinary,
    )
    assert ts._system_operation_authority_error("workspace_execution", developer) == ""
    assert ts._system_operation_authority_error("permission_mode_change", admin) == ""


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


def test_system_status_uses_projected_activity_and_shared_feed(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")
    ts.server.activity_tracker.reset_for_tests()
    response_ids = []
    for label in ("first", "second"):
        with ts.server.activity_tracker.response_span(
            "http-" + label, "private prose token=prompt-secret",
        ) as response:
            response_ids.append(response["id"])
            ts.server.activity_tracker.record_tool_result(
                "file_write",
                {"path": str(tmp_path / "private" / (label + ".txt")),
                 "content": "api_key=argument-secret"},
                output="CLIENT_SECRET=output-secret",
            )
            if label == "second":
                ts.server.activity_tracker.record_event(
                    "npu_fallback_handled",
                    capability="embeddings",
                    reason="ram_gate",
                    operation_mode="execution",
                    fallback_handler="ollama",
                    handler_state="handled",
                    raw_error="C:\\private\\model token=npu-secret",
                )
    monkeypatch.setattr(ts.server, "status", lambda: "ready")
    monkeypatch.setattr(ts.server, "sonder_stats", lambda: "stats")
    monkeypatch.setattr(ts.server, "learn_tiers", lambda: "tiers")
    monkeypatch.setattr(ts.server, "system_improvement_report", lambda: "ok")
    monkeypatch.setattr(ts.server, "context_health_data", lambda: {})
    monkeypatch.setattr(ts.server.context_policy, "policy", lambda *_: {})
    monkeypatch.setattr(ts.server.master_orchestrator, "snapshot", lambda: {
        "active_agents": 0, "running_agents": 0, "queued_agents": 0,
        "active_model_calls": 0,
    })
    monkeypatch.setattr(ts.server.autopilot_controller, "snapshot", lambda: {})
    monkeypatch.setattr(ts.server, "runtime_policy_data", lambda: {})
    monkeypatch.setattr(ts.server.selfmod, "status_data", lambda: {})
    monkeypatch.setattr(ts.server, "mcp_runtime_data", lambda: {})
    monkeypatch.setattr(ts.server, "learning_health_data", lambda: {})
    monkeypatch.setattr(ts.server.sonder_paths, "default_home", lambda: tmp_path)
    monkeypatch.setattr(ts.server, "available_tiers", lambda: {})
    monkeypatch.setattr(ts.server, "npu_fallback_status_data", lambda: {
        "schema_version": 1,
        "known": True,
        "capabilities": {
            "routing": {
                "policy_mode": "shadow", "role": "observer",
                "local_fallback_handler": "ollama",
            },
            "embeddings": {
                "policy_mode": "prefer", "role": "executor",
                "local_fallback_handler": "ollama",
            },
        },
        "last_fallback": {
            "capability": "embeddings", "reason": "ram_gate",
            "operation_mode": "execution", "fallback_handler": "ollama",
            "handler_state": "handled", "count": 1,
        },
        "reason_counts": {"ram_gate": 1},
    })

    with _http_server(monkeypatch) as port:
        status, _, body = _request(port, "GET", "/v1/sonder/status")

    text = body.decode("utf-8")
    payload = json.loads(text)
    assert status == 200
    assert payload["activity"]["projected"] is True
    assert payload["activity"]["detail_enabled"] is False
    assert payload["execution"]["feed"]["schema_version"] == 1
    assert payload["execution"]["feed"]["events"]
    assert payload["npu_fallback"]["last_fallback"] == {
        "capability": "embeddings", "reason": "ram_gate",
        "operation_mode": "execution", "fallback_handler": "ollama",
        "handler_state": "handled", "count": 1,
    }
    npu_event = next(
        row for row in payload["execution"]["feed"]["events"]
        if row["kind"] == "npu_fallback_handled"
    )
    assert npu_event["reason"] == "ram_gate"
    assert npu_event["fallback_handler"] == "ollama"
    feed_response_ids = {
        row["response_id"] for row in payload["execution"]["feed"]["events"]
    }
    assert set(response_ids).issubset(feed_response_ids)
    for secret in (
        "private prose", "prompt-secret", "argument-secret", "output-secret",
        "npu-secret", str(tmp_path / "private"),
    ):
        assert secret not in text


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


def test_http_developer_fanout_uses_authorized_internal_path(monkeypatch):
    """An API-key owner must not be rejected by MCP's token-only gate."""
    api_key = "k" * 32
    monkeypatch.setattr(ts, "API_KEY", api_key)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    calls = []
    monkeypatch.setattr(
        ts.server, "_model_fanout_authorized",
        lambda prompt, scope: calls.append((prompt, scope)) or '{"models_answered": 1}',
    )
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": "ask all local models: summarize this"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        )

    assert status == 200
    assert calls == [("summarize this", "local")]
    assert json.loads(body)["choices"][0]["message"]["content"].startswith('{"models_answered": 1}')


def test_http_todo_command_preserves_task_text_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "_DB_PATH", str(tmp_path / "http-task.db"))
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    monkeypatch.setattr(ts.server, "prewarm_model", lambda *args, **kwargs: None)
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": "/todo add HTTP visible task"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    content = json.loads(body)["choices"][0]["message"]["content"]
    assert status == 200
    assert content.startswith("task created\n  ")
    assert "HTTP visible task" in content


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


def test_chat_forwards_hosted_throttle_delay_and_explanation(monkeypatch):
    error = ts.server.ModelCallError(
        "http", "quota temporarily exhausted", status=429, cloud=True,
        retry_after_seconds=17,
    )
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
        ts.server, "answer_with_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    request = json.dumps({
        "model": "cloud-general",
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    payload = json.loads(body)
    assert status == 429
    assert headers["Retry-After"] == "17"
    assert "not retried automatically" in payload["error"]["message"]
    assert "after about 17s" in payload["error"]["message"]


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


def test_wrapped_dangerous_slash_is_denied_before_handler(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(
        ts, "_auth_account", lambda header: {"username": "u", "role": "user"}
    )
    called = []
    monkeypatch.setattr(ts, "_handle_slash", lambda *args, **kwargs: called.append(True))
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": "use model phi4: /run"}],
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
    assert status == 400
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


def test_deployment_gating_summary_covers_every_auth_mode(monkeypatch):
    """Every mode _effective_auth_mode() can return must have an authority line.

    This is the guard against _DEVELOPER_AUTHORITY_BY_MODE drifting out of step
    with _developer_authorized(): a new mode would otherwise render "unknown".
    """
    for mode in ("local-open", "api-key", "account", "both", "either"):
        monkeypatch.setattr(ts, "AUTH_MODE", mode)
        monkeypatch.setattr(ts, "API_KEY", "" if mode == "local-open" else "k" * 32)
        monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
        summary = ts._deployment_gating_summary()
        assert "effective auth mode : %s" % mode in summary
        assert "unknown" not in summary


def test_deployment_gating_summary_counts_conditionally_gated_names(monkeypatch):
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "API_KEY", "")
    summary = ts._deployment_gating_summary()
    # /autopilot and /auto are gated by action, not by set membership, so the
    # reported count must exceed the frozenset's size.
    expected = len(ts.DANGEROUS_HTTP_SLASH_COMMANDS | ts._CONDITIONALLY_GATED_SLASH)
    assert expected > len(ts.DANGEROUS_HTTP_SLASH_COMMANDS)
    assert "gated slash names   : %d" % expected in summary


def test_deployment_gating_summary_reports_the_bound_port(monkeypatch):
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "BOUND_PORT", 11439)
    assert "11439" in ts._deployment_gating_summary()


def test_deployment_gating_summary_warns_only_when_open(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    assert "holds developer" in ts._deployment_gating_summary()

    monkeypatch.setattr(ts, "API_KEY", "k" * 32)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    assert "holds developer" not in ts._deployment_gating_summary()


def test_bind_gate_tracks_the_named_minimum_key_length(monkeypatch):
    """The bind-time gate must read MIN_API_KEY_LENGTH, not a copy of its value.

    It was a bare literal 24, so raising the named constant would have hardened
    sonder_config.validate and left this gate -- the one that actually decides
    whether a non-loopback listener opens -- at the old minimum.
    """
    monkeypatch.setattr(sonder_config, "MIN_API_KEY_LENGTH", 40)

    with pytest.raises(RuntimeError):
        ts._validate_bind_security(
            "0.0.0.0", api_key="k" * 30, auth_mode="api-key", auth_secret=""
        )
    ts._validate_bind_security(
        "0.0.0.0", api_key="k" * 40, auth_mode="api-key", auth_secret=""
    )


def test_handler_declares_a_bounded_connection_timeout():
    """Handler declared no timeout, so StreamRequestHandler never set one and a
    stalled connection held its thread forever -- before any auth ran."""
    assert isinstance(ts.Handler.timeout, (int, float))
    assert 0 < ts.Handler.timeout <= 300


def test_stalled_connection_is_dropped_rather_than_holding_its_thread(monkeypatch):
    """Proves the mechanism the attribute above relies on: socketserver applies
    Handler.timeout to the connection and the stalled read closes it."""
    stalling = type("StallTimeoutHandler", (ts.Handler,), {"timeout": 0.5})
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), stalling)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        sock = socket.create_connection(("127.0.0.1", httpd.server_address[1]), timeout=10)
        try:
            # A request line with no terminating blank line: the server parks in
            # rfile.readline() waiting for headers that never come.
            sock.sendall(b"GET /v1/models HTTP/1.1\r\n")
            sock.settimeout(10)
            assert sock.recv(4096) == b"", "connection should be closed, not held open"
        finally:
            sock.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
