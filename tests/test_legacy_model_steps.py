"""Focused proof for the legacy model-step compatibility boundary."""

import io
import json
import types

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.security.prompt_provenance import PromptProvenanceBoundary, SourceKind
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session.model_steps import run_model_step, wrap_model_generator


def _capture(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db")
    return repository, SessionCaptureService(repository)


def test_model_step_preserves_request_options_and_redacts_provenance(tmp_path):
    repository, capture = _capture(tmp_path)
    boundary = PromptProvenanceBoundary()
    item = boundary.ingest(
        SourceKind.TOOL_RESULT,
        "secret-source-id",
        "grounded context",
        origin="https://private.example/token=secret",
    )
    packet = boundary.assemble_context((item,))
    binding = boundary.bind_model_request(
        "answer from context",
        system="standing system",
        history=({"role": "user", "content": "earlier"},),
        context=packet,
    )
    request = ModelRequest(
        prompt="answer from context",
        tier="code",
        system="standing system",
        history=({"role": "user", "content": "earlier"},),
        options={"temperature": 0.15, "num_predict": 77, "num_ctx": 8192},
        provenance=binding,
        context_packet=packet,
    )

    result = run_model_step(
        lambda: "answer",
        capture_factory=lambda: capture,
        session_id="session-1",
        request=request,
        user_message="answer from context",
    )

    assert result == "answer"
    events = repository.read_range("session-1")
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "model.response",
    ]
    payload = events[0].payload
    assert payload["options"] == {
        "temperature": 0.15, "num_predict": 77, "num_ctx": 8192,
    }
    assert payload["provenance"]["packet_digest"] == packet.packet_digest
    assert "secret-source-id" not in json.dumps(payload["provenance"])
    assert "private.example" not in json.dumps(payload["provenance"])


def test_model_step_captures_effective_provider_attempt_and_failure(tmp_path):
    repository, capture = _capture(tmp_path)
    request = ModelRequest(
        prompt="dispatch",
        tier="fast",
        options={"temperature": 0.2, "num_predict": 12},
    )

    from sonder_runtime.application.session.provider_attempts import dispatch_provider
    from sonder_runtime.adapters.model_transport import ModelCallError

    def invoke():
        return dispatch_provider(
            "ollama",
            "/api/chat",
            {"model": "fixture", "messages": [{"role": "user", "content": "dispatch"}]},
            lambda: "provider-answer",
        )

    assert run_model_step(
        invoke,
        capture_factory=lambda: capture,
        session_id="session-2",
        request=request,
    ) == "provider-answer"
    events = repository.read_range("session-2")
    assert [event.event_type for event in events] == [
        "model.requested", "provider.requested", "provider.responded", "model.response",
    ]
    assert events[1].payload["payload"]["model"] == "fixture"
    assert events[1].payload["request_id"] == events[0].payload["request_id"]

    def failed():
        return dispatch_provider(
            "ollama", "/api/chat", {"model": "fixture"},
            lambda: (_ for _ in ()).throw(ModelCallError("timeout", "private detail")),
        )

    with pytest.raises(ModelCallError):
        run_model_step(
            failed,
            capture_factory=lambda: capture,
            session_id="session-3",
            request=request,
        )
    failed_events = repository.read_range("session-3")
    assert [event.event_type for event in failed_events] == [
        "model.requested", "provider.requested", "provider.failed", "model.failed",
    ]
    assert "private detail" not in json.dumps([dict(event.payload) for event in failed_events])


def test_generator_wrapper_keeps_one_argument_legacy_call_shape(tmp_path):
    repository, capture = _capture(tmp_path)
    calls = []

    def legacy(prompt):
        calls.append(prompt)
        return "legacy answer"

    legacy.num_predict_override = 3

    wrapped = wrap_model_generator(
        legacy,
        capture_factory=lambda: capture,
        session_id="legacy-session",
        tier="general",
        options={"temperature": 0.2},
        options_factory=lambda _prompt, _history, raw: {
            "temperature": 0.2,
            "num_predict": raw.num_predict_override,
        },
        first_user_message="original task",
    )

    assert wrapped("model-visible prompt") == "legacy answer"
    assert calls == ["model-visible prompt"]
    events = repository.read_range("legacy-session")
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "model.response",
    ]
    assert events[1].payload["content"] == "original task"
    assert events[0].payload["options"]["num_predict"] == 3


def test_model_step_failure_mapper_falls_back_to_allowlisted_code(tmp_path):
    repository, capture = _capture(tmp_path)
    request = ModelRequest(prompt="failure", tier="fast")
    from sonder_runtime.application.session.provider_attempts import dispatch_provider

    def failed():
        return dispatch_provider(
            "ollama", "/api/chat", {"model": "fixture"},
            lambda: (_ for _ in ()).throw(RuntimeError("private detail")),
        )

    with pytest.raises(RuntimeError):
        run_model_step(
            failed,
            capture_factory=lambda: capture,
            session_id="mapped-failure-session",
            request=request,
            failure_code=lambda _error: "PRIVATE_CODE",
        )

    events = repository.read_range("mapped-failure-session")
    assert events[-1].payload["error_code"] == "INTERNAL_FAILURE"
    assert "private detail" not in json.dumps([dict(event.payload) for event in events])


def test_offload_explicit_session_uses_model_step_boundary(tmp_path, monkeypatch):
    import server

    repository, capture = _capture(tmp_path)
    monkeypatch.setattr(
        server, "_application",
        lambda: types.SimpleNamespace(session_capture_service=lambda: capture),
    )
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setitem(server.TIERS, "fast", "fixture")
    monkeypatch.setattr(server, "_auto_model_context", lambda *args: 2048)
    monkeypatch.setattr(
        server.ollama_endpoint,
        "open_url",
        lambda *args, **kwargs: io.BytesIO(
            b'{"message":{"content":"offload answer"}}'
        ),
    )

    assert server._offload_impl(
        "offload prompt",
        tier="fast",
        temperature=0.31,
        num_predict=19,
        num_ctx=2048,
        learn=False,
        session="offload-session",
    ) == "offload answer"
    events = repository.read_range("offload-session")
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "provider.requested",
        "provider.responded", "model.response",
    ]
    assert events[0].payload["options"]["temperature"] == 0.31
    assert events[0].payload["options"]["num_predict"] == 19
    assert events[0].payload["options"]["num_ctx"] == 2048
    provider_payload = events[2].payload["payload"]
    assert provider_payload["options"]["temperature"] == 0.31
    assert "headers" not in provider_payload
    assert "endpoint" not in provider_payload


def test_offload_explicit_session_records_failure_without_leaking_detail(tmp_path, monkeypatch):
    import urllib.error
    import server

    repository, capture = _capture(tmp_path)
    monkeypatch.setattr(
        server, "_application",
        lambda: types.SimpleNamespace(session_capture_service=lambda: capture),
    )
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setitem(server.TIERS, "fast", "fixture")
    monkeypatch.setattr(server, "_auto_model_context", lambda *args: 2048)
    monkeypatch.setenv("SONDER_LOCAL_RETRIES", "0")
    monkeypatch.setattr(
        server.ollama_endpoint,
        "open_url",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("private transport detail")
        ),
    )

    with pytest.raises(server.ModelCallError):
        server._offload_impl(
            "offload prompt",
            tier="fast",
            learn=False,
            session="failed-offload-session",
        )
    events = repository.read_range("failed-offload-session")
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "provider.requested",
        "provider.failed", "model.failed",
    ]
    assert events[-1].payload["error_code"] == "DEPENDENCY_UNAVAILABLE"
    assert "private transport detail" not in json.dumps([dict(event.payload) for event in events])


def test_agent_explicit_session_captures_each_decision(tmp_path, monkeypatch):
    import server

    repository, capture = _capture(tmp_path)
    monkeypatch.setattr(
        server, "_application",
        lambda: types.SimpleNamespace(session_capture_service=lambda: capture),
    )
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda *args, **kwargs: ("fixture", False, False, "code"),
    )
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "system")
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: False)
    monkeypatch.setenv("SONDER_SPECULATION", "0")
    responses = iter([
        '{"tool":"status","args":{},"reason":"inspect"}',
        '{"final":"done"}',
    ])
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *args, **kwargs: (lambda prompt, history=None: next(responses)),
    )
    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda *args, **kwargs: "status ok")

    assert server._agent_turn(
        "inspect",
        max_steps=2,
        read_only=True,
        session="agent-session",
    ) == "done"
    events = repository.read_range("agent-session")
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "model.response",
        "model.requested", "model.response",
    ]
    assert [event.payload["turn_id"] for event in events if event.event_type == "model.requested"] == [
        event.payload["turn_id"] for event in events if event.event_type == "model.response"
    ]
