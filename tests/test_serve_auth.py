from contextlib import contextmanager
import http.client
import io
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


def test_query_string_does_not_change_openai_route_or_terminal_metric(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "available_tiers", lambda: {})
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ts.server, "answer_with_history", lambda *args, **kwargs: "answer")
    request = json.dumps({
        "model": "sonder", "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions?trace=example", body=request,
            headers={"Content-Type": "application/json"},
        )
        models_status, _, models_body = _request(port, "GET", "/v1/models?trace=example")

    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"].startswith("answer")
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0
    assert models_status == 200
    assert json.loads(models_body)["object"] == "list"


@pytest.mark.parametrize("model", [None, 7, True, {}, []])
def test_chat_rejects_non_string_model_before_selector_routing(monkeypatch, model):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    request = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 400
    assert json.loads(body)["error"] == {
        "message": "model must be a string", "type": "invalid_request",
    }
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0


@pytest.mark.parametrize("stream", ["false", 1, {}, []])
def test_chat_rejects_non_boolean_stream_before_response_routing(monkeypatch, stream):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    request = json.dumps({
        "model": "sonder",
        "stream": stream,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 400
    assert json.loads(body)["error"] == {
        "message": "stream must be a boolean", "type": "invalid_request",
    }
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0


@pytest.mark.parametrize("response_format", [
    {"type": "json_schema", "json_schema": {"name": "result", "schema": {"type": "object"}}},
    {"type": "json_schema", "json_schema": {
        "name": "result", "strict": True,
        "schema": {"type": "number", "minimum": float("nan")},
    }},
    None,
    "json_object",
    [],
    {"type": "json_schema", "json_schema": {
        "name": "result", "strict": True,
        "schema": {"type": "array", "uniqueItems": True},
    }},
    {"type": "json_schema", "json_schema": {
        "name": "result", "strict": True,
        "schema": {
            "type": "array", "uniqueItems": True,
            "maxItems": ts._STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS + 1,
        },
    }},
])
def test_chat_rejects_invalid_response_format_before_routing(monkeypatch, response_format):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    calls = []
    monkeypatch.setattr(ts.server, "prewarm_model", lambda *_args: calls.append("prewarm"))
    monkeypatch.setattr(ts.server, "answer_with_history", lambda *_args, **_kwargs: calls.append("model") or "answer")
    request = json.dumps({
        "model": "sonder",
        "response_format": response_format,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 400
    assert json.loads(body)["error"]["type"] == "invalid_request"
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0
    assert calls == []


def test_response_format_accepts_an_arbitrarily_large_finite_integer_bound():
    schema = ts._response_format_schema({
        "type": "json_schema",
        "json_schema": {
            "name": "large_integer",
            "strict": True,
            "schema": {"type": "integer", "minimum": 10 ** 400},
        },
    })

    assert schema["minimum"] == 10 ** 400


def test_response_format_accepts_unique_items_at_the_host_safe_cap():
    schema = ts._response_format_schema({
        "type": "json_schema",
        "json_schema": {
            "name": "bounded_unique_array",
            "strict": True,
            "schema": {
                "type": "array",
                "uniqueItems": True,
                "maxItems": ts._STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS,
                "items": {"type": "integer"},
            },
        },
    })

    assert schema["maxItems"] == ts._STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS


def test_chat_response_format_uses_isolated_direct_model_path(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    seen = {}
    monkeypatch.setattr(ts.server, "prewarm_model", lambda *_args: None)
    monkeypatch.setattr(ts.server, "structured_answer_with_history", lambda prompt, history, schema, **kwargs: (
        seen.update(prompt=prompt, history=history, schema=schema, kwargs=kwargs) or '{"ok":true}'
    ))
    monkeypatch.setattr(ts, "_handle_slash", lambda *_args, **_kwargs: pytest.fail("control path ran"))
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *_args, **_kwargs: pytest.fail("web path ran"))
    request = json.dumps({
        "model": "sonder", "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": "return json"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(port, "POST", "/v1/chat/completions", body=request, headers={"Content-Type": "application/json"})

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["choices"][0]["message"]["content"] == '{"ok":true}'
    assert seen["schema"] == {"type": "object"}


def test_chat_response_format_rejects_slash_route_without_model_call(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "prewarm_model", lambda *_args: pytest.fail("prewarm ran"))
    request = json.dumps({
        "model": "sonder", "response_format": {"type": "json_object"},
        "messages": [{"role": "user", "content": "/status"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(port, "POST", "/v1/chat/completions", body=request, headers={"Content-Type": "application/json"})

    assert status == 400
    assert json.loads(body)["error"]["message"] == "response_format is unavailable for slash/tool/control routes"


def test_chat_response_format_streams_the_validated_direct_model_content(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "prewarm_model", lambda *_args: None)
    monkeypatch.setattr(ts.server, "structured_answer_with_history", lambda *_args, **_kwargs: '{"answer":"yes"}')
    request = json.dumps({
        "model": "sonder", "stream": True,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "answer", "strict": True,
            "schema": {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}, "additionalProperties": False},
        }},
        "messages": [{"role": "user", "content": "return json"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(port, "POST", "/v1/chat/completions", body=request, headers={"Content-Type": "application/json"})

    assert status == 200
    assert headers["Content-Type"].startswith("text/event-stream")
    assert '"content": "{\\"answer\\":\\"yes\\"}"' in body.decode("utf-8")


def test_chat_false_stream_returns_json_not_sse(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ts.server, "answer_with_history", lambda *args, **kwargs: "answer")
    request = json.dumps({
        "model": "sonder",
        "stream": False,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["choices"][0]["message"]["content"].startswith("answer")


def test_chat_normal_assistant_content_is_not_character_truncated(monkeypatch):
    """Receipt previews must not collapse a normal OpenAI response to `...`."""
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "prewarm_model", lambda *_args: None)
    answer = "begin:" + ("x" * 32_000) + ":end"
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ts.server, "answer_with_history", lambda *args, **kwargs: answer)
    request = json.dumps({
        "model": "sonder", "messages": [{"role": "user", "content": "long reply"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    # The normal conversation surface may append the configured activity
    # footer, but it must preserve the full assistant content before it.
    assert json.loads(body)["choices"][0]["message"]["content"].startswith(answer)


def test_chat_null_stream_uses_non_streaming_default(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(ts.server, "answer_with_history", lambda *args, **kwargs: "answer")
    request = json.dumps({
        "model": "sonder",
        "stream": None,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(body)["choices"][0]["message"]["content"].startswith("answer")


@pytest.mark.parametrize(
    "body, headers, expected_result",
    [
        (b"{not json", {"Content-Type": "application/json"}, "malformed_request"),
        (
            json.dumps({"model": "sonder", "messages": [{"role": "user", "content": "hello"}]}).encode("utf-8"),
            {"Content-Type": "application/json"},
            "unauthenticated",
        ),
    ],
)
def test_early_chat_failures_record_one_terminal_metric(
    monkeypatch, body, headers, expected_result,
):
    recorded = []
    monkeypatch.setattr(ts, "API_KEY", "required-key")
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(
        ts.Handler, "_record_chat_completion_metric",
        lambda _self, _lifecycle, result, _started: recorded.append(result),
    )

    with _http_server(monkeypatch) as port:
        status, _, _ = _request(
            port, "POST", "/v1/chat/completions", body=body, headers=headers,
        )

    assert status in (400, 401)
    assert recorded == [expected_result]


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


@pytest.mark.parametrize(
    "tool_name",
    [
        "set_context_size",
        "unload",
        "update_emotion_vectors",
        "tune_emotion_vectors",
        "learn_preference",
        "workflow_run",
    ],
)
def test_catalogued_global_controls_require_admin_on_shared_http(
    monkeypatch, tool_name,
):
    monkeypatch.setattr(
        ts.permission_modes, "decide_for_caller", lambda *_args, **_kwargs: None,
    )
    ordinary = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "ordinary", "role": "user"},
    }
    admin = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "admin", "role": "admin"},
    }
    local_open = {
        "mode": "local-open", "authorized": True, "api_key": False,
        "account": None,
    }

    assert "administrator" in ts._http_tool_refusal(
        (tool_name,), "/" + tool_name, context=ordinary,
    )
    assert ts._http_tool_refusal(
        (tool_name,), "/" + tool_name, context=admin,
    ) == ""
    assert ts._http_tool_refusal(
        (tool_name,), "/" + tool_name, context=local_open,
    ) == ""


@pytest.mark.parametrize(
    "command, mutation",
    (
        ("/emotion status", "set warmth=0.3"),
        ("/preferences list", "learn concise"),
        ("/contextsize", "1m"),
        ("/runtime status", "set code=example"),
        ("/models", "reset"),
    ),
)
def test_read_only_global_control_aliases_remain_available_to_shared_accounts(
    monkeypatch, command, mutation,
):
    monkeypatch.setattr(
        ts.permission_modes, "decide_for_caller", lambda *_args, **_kwargs: None,
    )
    ordinary = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "ordinary", "role": "user"},
    }
    cmd, _, argument = command.partition(" ")
    assert ts._http_slash_refusal(cmd, argument, context=ordinary) == ""
    assert "administrator" in ts._http_slash_refusal(
        cmd, mutation, context=ordinary,
    )


@pytest.mark.parametrize("action_type", ("emotion_update", "emotion_tune", "learn_preference", "unload"))
def test_loop_global_controls_require_admin_on_shared_http(action_type):
    ordinary = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "ordinary", "role": "user"},
    }
    admin = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "admin", "role": "admin"},
    }
    payload = json.dumps([{"type": action_type}])
    assert "administrator" in ts._loop_global_operation_refusal(payload, ordinary)
    assert ts._loop_global_operation_refusal(payload, admin) == ""


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


def test_served_source_update_requires_admin_but_check_remains_read_only(monkeypatch):
    developer = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "developer", "role": "developer"},
    }
    admin = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "admin", "role": "admin"},
    }
    monkeypatch.setattr(ts.server, "control_command", lambda prompt, **_kwargs: "ran " + prompt)

    for command in ("/update", "/updatesource apply"):
        refused = ts._handle_slash(command, context=developer)
        assert refused.startswith("refused "), refused
        assert "administrator" in refused
        assert ts._handle_slash(command, context=admin).startswith("ran ")

    assert ts._handle_slash("/updatecheck", context=developer).startswith("ran ")


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
        status, headers, body = _request(port, "GET", "/v1/sonder/status")

    text = body.decode("utf-8")
    payload = json.loads(text)
    assert status == 200
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0
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
        status, headers, body = _request(
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
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0


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
        status, headers, body = _request(
            port,
            "POST",
            "/v1/chat/completions",
            body=request,
            headers={"Content-Type": "application/json"},
    )

    assert status == 200
    payload = json.loads(body)
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0
    assert payload["sonder_elapsed_ms"] == int(headers["X-Sonder-Elapsed-Ms"])
    assert payload["choices"][0]["message"]["content"].startswith(
        "VALID ANSWER"
    )
    assert forwarded == [(
        "follow up",
        [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
        ],
    )]


def test_chat_success_receipt_uses_actual_generation_target(monkeypatch):
    """The receipt reports the accepted target, not the client selector."""
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)

    def fake_answer(prompt, history, *, target_observer=None, **kwargs):
        target_observer("actual-model:latest", "code", False)
        return "receipt answer"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)
    request = json.dumps({
        "model": "code", "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    payload = json.loads(body)
    receipt = payload["sonder_receipt"]
    assert receipt["request_id"].startswith("req_")
    assert headers["X-Sonder-Correlation-Id"] == receipt["request_id"]
    assert receipt["elapsed_ms"] == payload["sonder_elapsed_ms"]
    assert receipt["model"] == "actual-model:latest"
    assert receipt["tier"] == "code"
    assert "hello" not in json.dumps(receipt)


def test_models_response_includes_elapsed_header(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server, "available_tiers", lambda: {})

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(port, "GET", "/v1/models")

    assert status == 200
    assert json.loads(body)["object"] == "list"
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0


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
        lambda prompt, scope, **_kwargs: calls.append((prompt, scope)) or '{"models_answered": 1}',
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


def test_http_fanout_wrapper_is_not_reclassified_as_work_or_feedback(monkeypatch):
    """The extracted fanout question is data, not an independent control turn."""
    api_key = "k" * 32
    monkeypatch.setattr(ts, "API_KEY", api_key)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    monkeypatch.setattr(ts, "_handle_feedback", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("fanout must not reach feedback")))
    monkeypatch.setattr(ts, "_handle_intent", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("fanout must not reach work intent")))
    calls = []
    monkeypatch.setattr(
        ts.server, "_model_fanout_authorized",
        lambda prompt, scope, **_kwargs: calls.append((prompt, scope)) or '{"models_answered": 1}',
    )
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": "ask all available models to run this code"}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, _ = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        )

    assert status == 200
    assert calls == [("run this code", "all")]


@pytest.mark.parametrize("phrase", [
    "run python:3.12: reproduce this issue",
    "run python:3.12 model to reproduce this issue",
    "run using powershell:7 for reproduce this issue",
])
def test_http_bare_interpreter_tags_stay_on_normal_work_route(monkeypatch, phrase):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts.server, "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    prewarmed = []
    monkeypatch.setattr(ts.server, "prewarm_model", lambda model: prewarmed.append(model))
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *_args, **_kwargs: None)
    intents = []
    monkeypatch.setattr(
        ts, "_handle_intent", lambda content, **_kwargs: intents.append(content) or None,
    )
    monkeypatch.setattr(ts, "_handle_work_intent", lambda *_args, **_kwargs: None)
    seen = []
    monkeypatch.setattr(
        ts.server, "answer_with_history",
        lambda prompt, _history, **_kwargs: seen.append(prompt) or "answer",
    )
    request = json.dumps({
        "model": "sonder", "messages": [{"role": "user", "content": phrase}],
    }).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/chat/completions", body=request,
            headers={"Content-Type": "application/json"},
        )

    assert status == 200
    assert json.loads(body)["choices"][0]["message"]["content"].startswith("answer")
    assert prewarmed == [""]
    assert intents == [phrase]
    assert seen == [phrase]


def test_http_fanout_lifecycle_is_owner_scoped(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(
        ts, "_auth_account",
        lambda header: {"username": "dev-a" if "a-token" in header else "dev-b", "role": "developer"},
    )
    owner_a = ts._fanout_request_owner({"account": {"username": "dev-a"}, "api_key": False})
    run = {"id": "fan-owned", "request_owner": owner_a}
    monkeypatch.setattr(ts.server.fanout_store, "get_run", lambda _run_id: run)
    monkeypatch.setattr(ts.server, "_fanout_receipt", lambda run_id: {"run_id": run_id, "status": "completed"})

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "GET", "/v1/fanout/fan-owned",
            headers={"Authorization": "Bearer a-token"},
        )
        denied, _, denied_body = _request(
            port, "GET", "/v1/fanout/fan-owned",
            headers={"Authorization": "Bearer b-token"},
        )

    assert status == 200
    assert json.loads(body)["run_id"] == "fan-owned"
    assert denied == 404
    assert json.loads(denied_body)["error"]["type"] == "not_found"


def test_http_recent_fanout_summaries_are_owner_scoped(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(
        ts, "_auth_account",
        lambda header: {"username": "dev-a" if "a-token" in header else "dev-b", "role": "developer"},
    )
    captured = []
    monkeypatch.setattr(
        ts.server.fanout_store, "recent_run_summaries",
        lambda **kwargs: captured.append(kwargs) or [{"run_id": "fan-owned", "status": "completed"}],
    )

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "GET", "/v1/fanout?limit=7&include_finished=false",
            headers={"Authorization": "Bearer a-token"},
        )

    assert status == 200
    assert json.loads(body)["runs"][0]["run_id"] == "fan-owned"
    assert captured == [{
        "request_owner": ts._fanout_request_owner({"account": {"username": "dev-a"}, "api_key": False}),
        "include_finished": False, "limit": 7,
    }]


def test_http_recent_fanout_rejects_oversized_numeric_limit(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(
        ts.server.fanout_store, "recent_run_summaries",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not query store")),
    )

    with _http_server(monkeypatch) as port:
        status, _, body = _request(port, "GET", "/v1/fanout?limit=" + "9" * 5000)

    assert status == 400
    assert "limit must be an integer" in json.loads(body)["error"]["message"]


@pytest.mark.parametrize("query, message", [
    ("limit=1&limit=2", "limit must be supplied at most once"),
    ("include_finished=true&include_finished=false", "include_finished must be supplied at most once"),
])
def test_http_recent_fanout_rejects_duplicate_query_parameters(monkeypatch, query, message):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(
        ts.server.fanout_store, "recent_run_summaries",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not query store")),
    )

    with _http_server(monkeypatch) as port:
        status, _, body = _request(port, "GET", "/v1/fanout?" + query)

    assert status == 400
    assert json.loads(body)["error"]["message"] == message


def test_http_recent_fanout_admin_and_local_open_are_unscoped(monkeypatch):
    captured = []
    monkeypatch.setattr(
        ts.server.fanout_store, "recent_run_summaries",
        lambda **kwargs: captured.append(kwargs) or [{"run_id": "fan-operational"}],
    )

    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account", lambda _header: {"username": "admin", "role": "admin"})
    with _http_server(monkeypatch) as port:
        status, _, _ = _request(port, "GET", "/v1/fanout", headers={"Authorization": "Bearer admin-token"})
    assert status == 200
    assert captured.pop() == {"request_owner": None, "include_finished": True, "limit": 20}

    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    with _http_server(monkeypatch) as port:
        status, _, _ = _request(port, "GET", "/v1/fanout")
    assert status == 200
    assert captured.pop() == {"request_owner": None, "include_finished": True, "limit": 20}


def test_http_fanout_cancel_requires_developer_and_uses_owned_run(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account", lambda _header: {"username": "dev", "role": "developer"})
    owner = ts._fanout_request_owner({"account": {"username": "dev"}, "api_key": False})
    monkeypatch.setattr(ts.server.fanout_store, "get_run", lambda _run_id: {"id": "fan-owned", "request_owner": owner})
    cancelled = []
    monkeypatch.setattr(ts.server.fanout_store, "request_cancel", lambda run_id: cancelled.append(run_id) or {})
    monkeypatch.setattr(ts.server, "_fanout_receipt", lambda run_id: {"run_id": run_id, "status": "cancelled"})
    request = json.dumps({}).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/fanout/fan-owned/cancel", body=request,
            headers={"Content-Type": "application/json", "Authorization": "Bearer dev-token"},
        )

    assert status == 200
    assert cancelled == ["fan-owned"]
    assert json.loads(body)["status"] == "cancelled"


def test_fanout_owner_key_is_stable_without_raw_or_redactable_principal():
    context = {"account": {"username": "api_key=alice-secret", "role": "developer"}, "api_key": False}
    owner = ts._fanout_request_owner(context)

    assert owner.startswith("fo-")
    assert "alice" not in owner and "api_key" not in owner
    assert owner == ts._fanout_request_owner(context)
    assert ts._fanout_request_role(context) == "developer"


def test_http_fanout_resume_requires_literal_boolean(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account", lambda _header: {"username": "dev", "role": "developer"})
    owner = ts._fanout_request_owner({"account": {"username": "dev"}, "api_key": False})
    monkeypatch.setattr(ts.server.fanout_store, "get_run", lambda _run_id: {"id": "fan-owned", "request_owner": owner})
    monkeypatch.setattr(
        ts.server.fanout_store, "resume_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("resume must not run")),
    )
    request = json.dumps({"retry_unknown": "false"}).encode("utf-8")

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/fanout/fan-owned/resume", body=request,
            headers={"Content-Type": "application/json", "Authorization": "Bearer dev-token"},
        )

    assert status == 400
    assert "retry_unknown must be a boolean" in json.loads(body)["error"]["message"]


def test_http_fanout_synthesis_requires_owned_developer_receipt(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(
        ts, "_auth_account",
        lambda header: {"username": "dev-a" if "a-token" in header else "dev-b", "role": "developer"},
    )
    owner = ts._fanout_request_owner({"account": {"username": "dev-a"}, "api_key": False})
    run = {"id": "fan-owned", "request_owner": owner}
    monkeypatch.setattr(ts.server.fanout_store, "get_run", lambda _run_id: run)
    calls = []
    monkeypatch.setattr(
        ts.server, "_fanout_synthesize_run",
        lambda received, model: calls.append((received, model)) or {
            "run_id": received["id"], "synth_model": model or "local-code", "answer": "local synthesis",
        },
    )

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/fanout/fan-owned/synthesize",
            body=json.dumps({"synth_model": "local-code"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer a-token"},
        )
        denied, _, denied_body = _request(
            port, "POST", "/v1/fanout/fan-owned/synthesize", body=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Bearer b-token"},
        )

    assert status == 200
    assert json.loads(body)["answer"] == "local synthesis"
    assert calls == [(run, "local-code")]
    assert denied == 404
    assert json.loads(denied_body)["error"]["type"] == "not_found"


def test_http_fanout_synthesis_rejects_untyped_or_extra_payload_before_generation(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server.fanout_store, "get_run", lambda _run_id: {"id": "fan-local"})
    monkeypatch.setattr(
        ts.server, "_fanout_synthesize_run",
        lambda *_args: (_ for _ in ()).throw(AssertionError("generation must not run")),
    )

    with _http_server(monkeypatch) as port:
        wrong_type, _, wrong_body = _request(
            port, "POST", "/v1/fanout/fan-local/synthesize",
            body=json.dumps({"synth_model": ["not-a-model"]}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        extra, _, extra_body = _request(
            port, "POST", "/v1/fanout/fan-local/synthesize",
            body=json.dumps({"synth_model": "local", "retry_unknown": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    assert wrong_type == 400
    assert "synth_model must be a string" in json.loads(wrong_body)["error"]["message"]
    assert extra == 400
    assert "accepts only synth_model" in json.loads(extra_body)["error"]["message"]


def test_http_fanout_synthesis_maps_safe_model_failures(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.server.fanout_store, "get_run", lambda _run_id: {"id": "fan-local"})
    monkeypatch.setattr(
        ts.server, "_fanout_synthesize_run",
        lambda *_args: (_ for _ in ()).throw(ts.server.ModelCallError(
            "timeout", "local synthesis timed out", retry_after_seconds=0,
        )),
    )

    with _http_server(monkeypatch) as port:
        status, headers, body = _request(
            port, "POST", "/v1/fanout/fan-local/synthesize", body=b"{}",
            headers={"Content-Type": "application/json"},
        )

    assert status == 504
    assert headers["Retry-After"] == "0"
    assert json.loads(body)["error"] == {
        "message": "local synthesis timed out", "type": "server_error",
    }


def test_http_fanout_synthesis_applies_account_rate_limit(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    account = {"username": "dev", "role": "developer"}
    monkeypatch.setattr(ts, "_auth_account", lambda _header: account)
    owner = ts._fanout_request_owner({"account": account, "api_key": False})
    monkeypatch.setattr(
        ts.server.fanout_store, "get_run",
        lambda _run_id: {"id": "fan-owned", "request_owner": owner},
    )
    rate_calls = []
    monkeypatch.setattr(
        ts.admin_auth, "rate_limit",
        lambda _conn, received: rate_calls.append(received) or (False, "rate limit exceeded"),
    )
    monkeypatch.setattr(
        ts.server, "_fanout_synthesize_run",
        lambda *_args: (_ for _ in ()).throw(AssertionError("generation must not run")),
    )

    with _http_server(monkeypatch) as port:
        status, _, body = _request(
            port, "POST", "/v1/fanout/fan-owned/synthesize", body=b"{}",
            headers={"Content-Type": "application/json", "Authorization": "Bearer dev-token"},
        )

    assert status == 429
    assert json.loads(body)["error"] == {"message": "rate limit exceeded", "type": "rate_limit"}
    assert rate_calls == [account]


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
    assert int(headers["X-Sonder-Elapsed-Ms"]) >= 0
    assert "choices" not in payload


def test_stream_exposes_same_elapsed_header_as_terminal_chunk():
    class StreamProbe:
        def __init__(self):
            self.headers = {}
            self.wfile = io.BytesIO()
            self.close_connection = False

        def send_response(self, status):
            assert status == 200

        def _cors(self):
            pass

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

    probe = StreamProbe()
    probe._correlation_id = "req_stream"
    receipt = {
        "request_id": "req_stream", "elapsed_ms": 37,
        "model": "actual-model:latest", "tier": "code",
    }
    assert ts.Handler._send_stream(
        probe, "hello", "sonder", iid="stream", elapsed_ms=37,
        receipt=receipt,
    )

    body = probe.wfile.getvalue().decode("utf-8")
    assert probe.headers["X-Sonder-Elapsed-Ms"] == "37"
    assert probe.headers["X-Sonder-Correlation-Id"] == "req_stream"
    assert '"sonder_elapsed_ms": 37' in body
    assert '"sonder_receipt": {"request_id": "req_stream"' in body
    assert body.endswith("data: [DONE]\n\n")


def test_stream_preserves_long_normal_assistant_content():
    class StreamProbe:
        def __init__(self):
            self.headers = {}
            self.wfile = io.BytesIO()
            self.close_connection = False

        def send_response(self, status):
            assert status == 200

        def _cors(self):
            pass

        def send_header(self, name, value):
            self.headers[name] = value

        def end_headers(self):
            pass

    answer = "begin:" + ("x" * 200_000) + ":end"
    probe = StreamProbe()
    probe._correlation_id = "req_stream_long"

    assert ts.Handler._send_stream(probe, answer, "sonder", iid="stream-long")

    payloads = [
        json.loads(line[6:])
        for line in probe.wfile.getvalue().decode("utf-8").splitlines()
        if line.startswith("data: {")
    ]
    assert payloads[0]["choices"][0]["delta"]["content"] == answer


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
        assert headers["Access-Control-Expose-Headers"] == (
            "X-Sonder-Elapsed-Ms, X-Sonder-Correlation-Id"
        )
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
        "use model phi4: /run",
        "run using phi4:latest: /run",
        "ask all available models for /run",
        "ask with qwen2.5-coder:14b to /run",
    ],
)
def test_wrapped_dangerous_slash_is_denied_before_handler(monkeypatch, prompt):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(
        ts, "_auth_account", lambda header: {"username": "u", "role": "user"}
    )
    called = []
    monkeypatch.setattr(ts, "_handle_slash", lambda *args, **kwargs: called.append(True))
    request = json.dumps({
        "model": "sonder",
        "messages": [{"role": "user", "content": prompt}],
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
