from __future__ import annotations

from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.session import SessionCaptureService
from sonder_runtime.domain.common.ids import SessionId, TurnId


class _Gateway:
    def generate(self, request, context):
        return ModelResponse(
            text=f"answer:{request.prompt}", model="test-model", tier=request.tier,
        )


def test_canonical_chat_captures_typed_ids_and_model_response(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    capture = SessionCaptureService(repository, replay_limit=100)
    session_id = SessionId.new()
    turn_id = TurnId.new()

    result = ChatService(_Gateway(), capture).complete(
        ChatCommand(
            content="remember this",
            tier="code",
            session_id=session_id,
            turn_id=turn_id,
        ),
        local_owner_context(correlation_id="chat-capture", source="test"),
    )

    assert result.capture is not None
    assert result.capture.session_id == session_id.serialize()
    assert result.capture.turn_id == turn_id.serialize()
    assert [event.event_type for event in result.capture.appended] == [
        "model.requested", "user.message", "model.response",
    ]
    assert result.capture.replay.request is not None
    assert result.capture.replay.request.request_id.startswith("request_")
    assert result.capture.replay.replay.transcript[-1].content == "answer:remember this"
    assert result.capture.export.integrity is not None
    assert result.capture.export.integrity.valid


def test_canonical_chat_replay_is_deterministic_for_the_captured_stream(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    capture = SessionCaptureService(repository, replay_limit=100)
    result = ChatService(_Gateway(), capture).complete(
        ChatCommand(content="same durable facts"),
        local_owner_context(correlation_id="chat-replay", source="test"),
    )

    assert result.capture is not None
    from sonder_runtime.application.session.durable_replay import crash_safe_replay

    replay_again = crash_safe_replay(
        repository, result.capture.session_id, max_events=100,
    )
    assert replay_again.replay == result.capture.replay.replay
    assert replay_again.integrity == result.capture.replay.integrity
    assert repository.read_range(result.capture.session_id, limit=100) == (
        result.capture.appended
    )
