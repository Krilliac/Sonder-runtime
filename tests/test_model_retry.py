import io
import importlib
import http.client
import urllib.error

import pytest

import memory_store
import orchestrator
import server


def test_model_error_identity_survives_server_reload():
    error_type = server.ModelCallError
    old_error = error_type("transport", "in flight")

    importlib.reload(server)

    assert server.ModelCallError is error_type
    assert isinstance(old_error, server.ModelCallError)


def _http_error(code, body=b'{"error":"request rejected"}'):
    return urllib.error.HTTPError(
        server.BASE + "/api/chat",
        code,
        "failure",
        {},
        io.BytesIO(body),
    )


def test_transient_local_failure_retries_once_under_original_budget(monkeypatch):
    calls = []

    def fake_post(path, payload, timeout=None):
        calls.append(timeout)
        if len(calls) == 1:
            raise urllib.error.URLError(ConnectionResetError("reset"))
        return {"message": {"content": "recovered"}}

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "1")
    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "0")
    monkeypatch.setattr(server, "_post", fake_post)

    out, content = server._chat_request(
        {"model": "local", "messages": []},
        model="local",
        timeout=17,
    )

    assert content == "recovered"
    assert out["message"]["content"] == "recovered"
    assert len(calls) == 2
    assert 1 <= calls[1] <= calls[0] == 17


def test_incomplete_response_read_is_a_bounded_transient_retry(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise http.client.IncompleteRead(b"partial", 100)
        return {"message": {"content": "complete"}}

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "1")
    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "0")
    monkeypatch.setattr(server, "_post", fake_post)

    _, content = server._chat_request({}, model="local", timeout=20)

    assert content == "complete"
    assert len(calls) == 2


@pytest.mark.parametrize("status", [400, 401, 404, 500])
def test_terminal_http_failures_do_not_retry(monkeypatch, status):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        raise _http_error(status)

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "2")
    monkeypatch.setattr(server, "_post", fake_post)

    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request({}, model="local", timeout=20)

    assert caught.value.status == status
    assert caught.value.attempts == 1
    assert len(calls) == 1


def test_transient_cloud_failure_is_never_retried(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        raise _http_error(503, b'{"error":"temporarily unavailable"}')

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "2")
    monkeypatch.setattr(server, "_post", fake_post)

    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request({}, model="hosted", cloud=True, timeout=20)

    assert caught.value.cloud is True
    assert caught.value.status == 503
    assert len(calls) == 1


def test_cloud_model_name_is_fail_safe_single_attempt(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        raise urllib.error.URLError(ConnectionResetError("reset"))

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "2")
    monkeypatch.setattr(server, "_post", fake_post)

    with pytest.raises(server.ModelCallError) as caught:
        server._generate_text("hello", tier="cloud-code")

    assert caught.value.cloud is True
    assert caught.value.attempts == 1
    assert len(calls) == 1


def test_cloud_model_name_cannot_bypass_cloud_opt_in(monkeypatch):
    calls = []
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    monkeypatch.setattr(
        server, "_post", lambda *args, **kwargs: calls.append(1) or {},
    )

    with pytest.raises(server.ModelCallError) as caught:
        server._generate_text("hello", tier="cloud-code")

    assert caught.value.kind == "configuration"
    assert caught.value.attempts == 0
    assert caught.value.cloud is True
    assert calls == []


def test_cancellation_between_attempts_suppresses_retry(monkeypatch):
    calls = []
    cancelled = {"value": False}

    def fake_post(*args, **kwargs):
        calls.append(1)
        cancelled["value"] = True
        raise urllib.error.URLError(ConnectionRefusedError("offline"))

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "1")
    monkeypatch.setenv("SONDER_LOCAL_RETRY_DELAY_MS", "0")
    monkeypatch.setattr(server, "_post", fake_post)

    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request(
            {}, model="local", timeout=20,
            cancel_check=lambda: cancelled["value"],
        )

    assert caught.value.kind == "cancelled"
    assert len(calls) == 1


def test_preexisting_cancellation_suppresses_first_attempt(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server, "_post", lambda *args, **kwargs: calls.append(1) or {},
    )

    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request(
            {}, model="local", timeout=20, cancel_check=lambda: True,
        )

    assert caught.value.kind == "cancelled"
    assert caught.value.attempts == 0
    assert calls == []


@pytest.mark.parametrize(
    "reply",
    [
        {},
        {"message": {}},
        {"message": {"content": ""}},
        {"message": {"content": 42}},
        {"error": "model unavailable"},
    ],
)
def test_invalid_or_empty_reply_is_not_captured(monkeypatch, reply):
    monkeypatch.setattr(server, "_post", lambda *args, **kwargs: reply)
    conn = memory_store.connect()
    memory_store.init_db(conn)
    gen = server._make_generate("local", "", 0.2, 32, 2048)

    with pytest.raises(server.ModelCallError):
        orchestrator.run_with_learning(conn, "do work", "code", gen)

    assert memory_store.count_interactions(conn) == 0
    conn.close()


def test_public_offload_formats_http_failure_without_server_down_claim(monkeypatch):
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "0")
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            _http_error(400, b'{"error":"context length exceeded"}')
        ),
    )

    result = server.offload("too large", tier="fast", learn=False)

    assert "HTTP 400" in result
    assert "context length exceeded" in result
    assert "server running" not in result.lower()


def test_structured_http_error_redacts_nested_secret_values(monkeypatch):
    body = (
        b'{"error":{"message":"denied","token":"supersecret",'
        b'"nested":{"api_key":"sk-private"}}}'
    )
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "0")
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(_http_error(400, body)),
    )

    result = server.offload("fail safely", tier="fast", learn=False)

    assert "supersecret" not in result
    assert "sk-private" not in result
    assert result.count("<redacted>") == 2


def test_structured_http_error_without_error_key_is_also_redacted(monkeypatch):
    body = (
        b'{"message":"denied","token":"supersecret",'
        b'"nested":{"api_key":"sk-private"}}'
    )
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "0")
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: (_ for _ in ()).throw(_http_error(400, body)),
    )

    result = server.offload("fail safely", tier="fast", learn=False)

    assert "supersecret" not in result
    assert "sk-private" not in result
    assert result.count("<redacted>") == 2


def test_malformed_optional_usage_counts_fall_back_to_estimates(monkeypatch):
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: {
            "message": {"content": "valid answer"},
            "prompt_eval_count": "not-a-number",
            "eval_count": -5,
        },
    )

    assert server.offload("hello", tier="fast", learn=False) == "valid answer"


def test_oversized_model_response_is_rejected_without_retry(monkeypatch):
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return b"x" * limit

    def fake_urlopen(*args, **kwargs):
        calls.append(1)
        return Response()

    monkeypatch.setattr(server, "_MAX_MODEL_RESPONSE_BYTES", 32)
    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request({}, model="local", timeout=20)

    assert caught.value.kind == "protocol"
    assert "safety limit" in caught.value.detail
    assert len(calls) == 1
