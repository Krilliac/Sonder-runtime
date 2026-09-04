import json

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.domain.common.errors import IntegrityFailure


def owner(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    pending = capture.begin_request("session", "turn", ModelRequest(prompt="logical", tier="code"), request_id="request")
    return repository, capture, pending


def test_provider_payload_committed_before_dispatch_and_not_transcript(tmp_path):
    from sonder_runtime.application.session.provider_attempts import provider_attempt_scope, dispatch_provider

    repository, capture, pending = owner(tmp_path)
    payload = {"model": "resolved", "messages": [{"role": "user", "content": "augmented"}], "think": False}

    def send():
        reopened = SQLiteSessionRepository(tmp_path / "session.db")
        events = reopened.read_range("session")
        assert events[-1].event_type == "provider.requested"
        assert events[-1].payload["payload"] == payload
        assert events[-1].payload["request_id"] == "request"
        return {"message": {"content": "candidate"}}

    with provider_attempt_scope(capture, pending):
        result = dispatch_provider("ollama", "/api/chat", payload, send)
    result["message"]["content"] = "mutated"
    capture.complete_request(pending, model_response="selected")
    events = repository.read_range("session")
    assert events[2].payload["response"]["message"]["content"] == "candidate"
    assert events[1].payload["attempt_id"] == events[2].payload["attempt_id"]
    assert [m.content for m in capture.replay("session").replay.transcript] == ["selected"]


@pytest.mark.parametrize("failure", [RuntimeError("private detail"), SystemExit("interrupted")])
def test_failure_or_interruption_keeps_truthful_evidence(tmp_path, failure):
    from sonder_runtime.application.session.provider_attempts import provider_attempt_scope, dispatch_provider

    repository, capture, pending = owner(tmp_path)
    with provider_attempt_scope(capture, pending), pytest.raises(type(failure)):
        dispatch_provider("ollama", "/api/chat", {}, lambda: (_ for _ in ()).throw(failure))
    events = repository.read_range("session")
    assert events[-1].event_type == ("provider.failed" if isinstance(failure, Exception) else "provider.requested")
    assert "private detail" not in json.dumps([dict(e.payload) for e in events])


@pytest.mark.parametrize("event_type,expected_calls", [("provider.requested", 0), ("provider.responded", 1)])
def test_storage_failure_is_integrity_failure_without_rerun(tmp_path, event_type, expected_calls):
    from sonder_runtime.application.session.provider_attempts import provider_attempt_scope, dispatch_provider

    repository, capture, pending = owner(tmp_path)
    append = repository.append
    calls = []

    def failing_append(session, kind, payload, **kwargs):
        if kind == event_type:
            raise OSError("private storage path")
        return append(session, kind, payload, **kwargs)

    repository.append = failing_append
    with pytest.raises(IntegrityFailure), provider_attempt_scope(capture, pending):
        dispatch_provider("ollama", "/api/chat", {}, lambda: calls.append(1) or {})
    assert len(calls) == expected_calls


def test_typed_openai_captures_resolved_body_without_headers(tmp_path):
    from sonder_runtime.adapters.inference.openai_compat_gateway import OpenAICompatibleConfig, OpenAICompatibleGateway
    from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService
    from sonder_runtime.application.context import local_owner_context
    from sonder_runtime.domain.common.ids import SessionId

    repository = SQLiteSessionRepository(tmp_path / "session.db")
    session = SessionId.new()

    def send(url, payload, headers, timeout):
        events = repository.read_range(session.serialize())
        assert events[-1].event_type == "provider.requested"
        assert events[-1].payload["payload"] == payload
        assert payload["model"] == "resolved-model"
        assert payload["messages"] == [{"role": "system", "content": "system"}, {"role": "user", "content": "hello"}]
        return {"choices": [{"message": {"content": "answer"}}]}

    gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(base_url="http://127.0.0.1:8080", model="resolved-model", api_key="fixture-secret"), transport=send)
    result = ChatService(gateway, SessionCaptureService(repository)).complete(
        ChatCommand(content="hello", system="system", session_id=session), local_owner_context(correlation_id="test"))
    assert result.response_text == "answer"
    events = repository.read_range(session.serialize())
    assert "fixture-secret" not in json.dumps([dict(e.payload) for e in events])
    assert [e.event_type for e in events] == ["model.requested", "user.message", "provider.requested", "provider.responded", "model.response"]
    assert [m.content for m in result.capture.replay.replay.transcript] == ["hello", "answer"]


def test_ollama_think_retry_captures_each_effective_payload(tmp_path, monkeypatch):
    import io
    import urllib.error
    import server
    from sonder_runtime.application.session.provider_attempts import provider_attempt_scope

    repository, capture, pending = owner(tmp_path)
    calls = []

    def send(req, **kwargs):
        body = json.loads(req.data)
        calls.append(body)
        assert repository.read_range("session")[-1].payload["payload"] == body
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 400, "failure", {}, io.BytesIO(b'{"error":"model does not support thinking"}'))
        return io.BytesIO(b'{"message":{"content":"compatible"}}')

    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "0")
    monkeypatch.setattr(server.ollama_endpoint, "open_url", send)
    with provider_attempt_scope(capture, pending):
        _, content = server._chat_request({"model": "fixture", "messages": [], "think": True}, model="fixture", timeout=20)
    assert content == "compatible"
    events = repository.read_range("session")
    assert [e.event_type for e in events] == ["model.requested", "provider.requested", "provider.failed", "provider.requested", "provider.responded"]
    assert events[1].payload["attempt_id"] != events[3].payload["attempt_id"]
    assert "think" in events[1].payload["payload"]
    assert "think" not in events[3].payload["payload"]


def test_swallowed_capture_error_cannot_dispatch_again_or_return_success(tmp_path):
    from sonder_runtime.application.session.provider_attempts import provider_attempt_scope, dispatch_provider

    repository, capture, pending = owner(tmp_path)
    append = repository.append
    calls = []

    def failing_append(session, kind, payload, **kwargs):
        if kind == "provider.responded":
            raise OSError("fixture")
        return append(session, kind, payload, **kwargs)

    repository.append = failing_append
    with pytest.raises(IntegrityFailure), provider_attempt_scope(capture, pending):
        for _ in range(2):
            try:
                dispatch_provider("ollama", "/api/chat", {}, lambda: calls.append(1) or {})
            except Exception:
                pass
    assert calls == [1]


def test_pool_failover_has_distinct_evidence_and_capture_failure_never_fails_over(tmp_path, monkeypatch):
    import io
    import urllib.error
    import server
    from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool
    from sonder_runtime.application.session.provider_attempts import provider_attempt_scope

    repository, capture, pending = owner(tmp_path)
    pool = OllamaWorkerPool("http://127.0.0.1:11434", ("http://127.0.0.1:11435",), allow_remote=False)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    calls = []

    def send(req, **kwargs):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise urllib.error.URLError(ConnectionResetError("fixture"))
        return io.BytesIO(b'{"message":{"content":"answer"}}')

    monkeypatch.setattr(server.ollama_endpoint, "open_url", send)
    with provider_attempt_scope(capture, pending):
        server._post("/api/chat", {"messages": []}, idempotent=True)
    events = repository.read_range("session")
    assert len(calls) == 2 and calls[0] != calls[1]
    assert [e.event_type for e in events] == ["model.requested", "provider.requested", "provider.failed", "provider.requested", "provider.responded"]
    assert events[1].payload["attempt_id"] != events[3].payload["attempt_id"]

    calls.clear()
    monkeypatch.setattr(capture, "begin_provider_attempt", lambda *a, **k: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(IntegrityFailure), provider_attempt_scope(capture, pending):
        server._post("/api/chat", {"messages": []}, idempotent=True)
    assert calls == []


def test_oversized_admission_stops_before_transport_and_scope_resets(tmp_path):
    from sonder_runtime.application.session.provider_attempts import provider_attempt_scope, dispatch_provider

    _, capture, pending = owner(tmp_path)
    calls = []
    with pytest.raises(IntegrityFailure), provider_attempt_scope(capture, pending):
        dispatch_provider("ollama", "/api/chat", {"prompt": "x" * 2_000_001}, lambda: calls.append(1))
    assert calls == []
    assert dispatch_provider("ollama", "/api/chat", {}, lambda: "unconfigured") == "unconfigured"

