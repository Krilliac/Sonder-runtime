from __future__ import annotations

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.chat.handle_chat import ChatCommand
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.security.prompt_provenance import PromptProvenanceBoundary
from sonder_runtime.application.session import SessionCaptureService
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.ids import SessionId, TurnId
from sonder_runtime.interfaces.http import serve
from sonder_runtime.interfaces.http.facades.session import dispatch_session_route


class _Gateway:
    def generate(self, request, context):
        return ModelResponse(
            text=f"answer:{request.prompt}", model="test-model", tier=request.tier,
        )


def test_canonical_application_graph_replays_after_database_reopen(tmp_path, monkeypatch):
    database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    session_id = SessionId.new()

    first = bootstrap_app.build_application()
    first.chat._gateway = _Gateway()
    result = first.chat.complete(
        ChatCommand(
            content="restart me", session_id=session_id, turn_id=TurnId.new(),
        ),
        local_owner_context(correlation_id="session-restart", source="test"),
    )
    assert result.capture is not None
    assert first.session_capture_service() is first.session_capture_service()

    reopened = bootstrap_app.build_application()
    replay = reopened.session_capture_service().replay(session_id.serialize())

    assert replay.crash_safe
    assert replay.request is not None
    assert replay.request.request.prompt == "restart me"
    assert replay.replay.transcript[-1].content == "answer:restart me"


def test_canonical_session_capture_does_not_persist_raw_provenance(tmp_path):
    item = PromptProvenanceBoundary().ingest(
        "web_result", "secret-source-id", "untrusted source bytes",
        origin="https://user:password@example.test/private",
    )
    packet = PromptProvenanceBoundary().assemble_context((item,))
    request = ModelRequest(
        prompt="Use the source", tier="code", context_packet=packet,
        provenance=PromptProvenanceBoundary().bind_model_request(
            "Use the source", context=packet,
        ),
    )
    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    SessionCaptureService(repository, replay_limit=100).capture_turn(
        "privacy", "turn-1", request, request_id="request-1", model_response="done",
    )

    payloads = [event.payload for event in repository.read_range("privacy", limit=100)]
    encoded = repr(payloads)
    assert "secret-source-id" not in encoded
    assert "user:password" not in encoded
    assert "untrusted source bytes" not in encoded
    assert payloads[0]["provenance"]["item_count"] == 1


def test_live_http_session_facade_uses_application_repository_after_restart(tmp_path, monkeypatch):
    database = tmp_path / "http-session.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    bootstrap_app.reset_for_tests()
    first = bootstrap_app.build_application()
    session_id = SessionId.new()
    first.chat._gateway = _Gateway()
    first.chat.complete(
        ChatCommand(
            content="http durable turn", session_id=session_id, turn_id=TurnId.new(),
        ),
        local_owner_context(correlation_id="http-session", source="test"),
    )

    facade = first.session_http_facade()
    assert facade is first.session_http_facade()
    assert serve.configure_session_facade(facade) is facade
    route = dispatch_session_route(
        serve._SESSION_FACADE,
        f"/v1/sessions/{session_id.serialize()}/replay",
    )
    assert route.status_code == 200
    assert route.body["integrity_valid"] is True

    bootstrap_app.reset_for_tests()
    reopened = bootstrap_app.build_application()
    reopened_route = dispatch_session_route(
        reopened.session_http_facade(),
        f"/v1/sessions/{session_id.serialize()}/trajectory",
    )
    assert reopened_route.status_code == 200
    assert reopened_route.body["session_id"] == session_id.serialize()
