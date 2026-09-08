from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.session import SessionCaptureService
from sonder_runtime.domain.common.ids import SessionId, TurnId
from sonder_runtime.domain.common.errors import (
    Cancelled, DeadlineExceeded, DependencyUnavailable, IntegrityFailure,
)


class _Gateway:
    def generate(self, request, context):
        return ModelResponse(
            text=f"answer:{request.prompt}", model="test-model", tier=request.tier,
        )


def test_request_is_committed_before_gateway_dispatch(tmp_path):
    database = tmp_path / "session.db"
    repository = SQLiteSessionRepository(database, max_read_limit=100)
    capture = SessionCaptureService(repository, replay_limit=100)
    session_id = SessionId.new()

    class InspectingGateway:
        def generate(self, request, context):
            reopened = SQLiteSessionRepository(database, max_read_limit=100)
            events = reopened.read_range(session_id.serialize(), limit=100)
            assert [e.event_type for e in events] == [
                "model.requested", "user.message",
            ]
            assert events[0].payload["prompt"] == request.prompt
            return ModelResponse(text="checked", model="fake", tier=request.tier)

    result = ChatService(InspectingGateway(), capture).complete(
        ChatCommand(content="inspect this repository", session_id=session_id),
        local_owner_context(correlation_id="before-dispatch", source="system"),
    )
    assert result.response_text == "checked"


@pytest.mark.parametrize("error,code", [
    (DependencyUnavailable("fixture provider failure"), "DEPENDENCY_UNAVAILABLE"),
    (Cancelled("private cancellation detail"), "CANCELLED"),
    (DeadlineExceeded("private deadline detail"), "DEADLINE_EXCEEDED"),
    (RuntimeError("private unexpected detail"), "INTERNAL_FAILURE"),
])
def test_model_failure_is_correlated_and_original_exception_propagates(tmp_path, error, code):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    session_id, turn_id = SessionId.new(), TurnId.new()

    class FailingGateway:
        def generate(self, request, context):
            raise error

    with pytest.raises(type(error)) as caught:
        ChatService(FailingGateway(), capture).complete(
            ChatCommand(content="hello", session_id=session_id, turn_id=turn_id),
            local_owner_context(correlation_id="failure", source="test"),
        )
    assert caught.value is error
    events = repository.read_range(session_id.serialize())
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "model.failed",
    ]
    assert events[-1].payload == {
        "request_id": events[0].payload["request_id"],
        "turn_id": turn_id.serialize(), "error_code": code,
    }


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


class _CountingGateway(_Gateway):
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def generate(self, request, context):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return super().generate(request, context)


class _FailingAppendRepository(SQLiteSessionRepository):
    def __init__(self, database, fail_event):
        super().__init__(database)
        self.fail_event = fail_event
        self.error = RuntimeError("fixture storage failure")

    def append(self, session_id, event_type, payload, **kwargs):
        if event_type == self.fail_event:
            raise self.error
        return super().append(session_id, event_type, payload, **kwargs)


def test_capture_factory_failure_prevents_dispatch():
    gateway = _CountingGateway()
    error = RuntimeError("fixture initialization failure")

    def failing_factory():
        raise error

    with pytest.raises(RuntimeError) as caught:
        ChatService(gateway, session_capture_factory=failing_factory).complete(
            ChatCommand(content="hello"),
            local_owner_context(correlation_id="factory-failure", source="test"),
        )
    assert caught.value is error
    assert gateway.calls == 0


@pytest.mark.parametrize("fail_event,prefix", [
    ("model.requested", []), ("user.message", ["model.requested"]),
])
def test_admission_write_failure_prevents_dispatch_and_keeps_prefix(tmp_path, fail_event, prefix):
    database = tmp_path / "session.db"
    repository = _FailingAppendRepository(database, fail_event)
    gateway = _CountingGateway()
    session_id = SessionId.new()
    with pytest.raises(RuntimeError) as caught:
        ChatService(gateway, SessionCaptureService(repository)).complete(
            ChatCommand(content="hello", session_id=session_id),
            local_owner_context(correlation_id="admission-failure", source="test"),
        )
    assert caught.value is repository.error
    assert gateway.calls == 0
    reopened = SQLiteSessionRepository(database)
    assert [event.event_type for event in reopened.read_range(session_id.serialize())] == prefix


def test_completion_write_failure_does_not_retry_or_record_model_failure(tmp_path):
    repository = _FailingAppendRepository(tmp_path / "session.db", "model.response")
    gateway = _CountingGateway()
    session_id = SessionId.new()
    with pytest.raises(RuntimeError) as caught:
        ChatService(gateway, SessionCaptureService(repository)).complete(
            ChatCommand(content="hello", session_id=session_id),
            local_owner_context(correlation_id="completion-failure", source="test"),
        )
    assert caught.value is repository.error
    assert gateway.calls == 1
    assert [event.event_type for event in repository.read_range(session_id.serialize())] == [
        "model.requested", "user.message",
    ]


def test_failure_write_failure_reports_integrity_failure(tmp_path):
    repository = _FailingAppendRepository(tmp_path / "session.db", "model.failed")
    gateway = _CountingGateway(DependencyUnavailable("private provider message"))
    session_id = SessionId.new()
    with pytest.raises(IntegrityFailure, match="^could not persist model failure$") as caught:
        ChatService(gateway, SessionCaptureService(repository)).complete(
            ChatCommand(content="hello", session_id=session_id),
            local_owner_context(correlation_id="failure-write", source="test"),
        )
    assert caught.value.__cause__ is repository.error
    assert gateway.calls == 1
    assert [event.event_type for event in repository.read_range(session_id.serialize())] == [
        "model.requested", "user.message",
    ]


def test_process_termination_leaves_request_unresolved(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    error = SystemExit("fixture abrupt termination")
    gateway = _CountingGateway(error)
    session_id = SessionId.new()
    with pytest.raises(SystemExit) as caught:
        ChatService(gateway, SessionCaptureService(repository)).complete(
            ChatCommand(content="hello", session_id=session_id),
            local_owner_context(correlation_id="abrupt-exit", source="test"),
        )
    assert caught.value is error
    assert [event.event_type for event in repository.read_range(session_id.serialize())] == [
        "model.requested", "user.message",
    ]


def test_factory_capture_preserves_request_options_context_and_new_attempt_ids(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    context = local_owner_context(correlation_id="options", source="test")
    history = ({"role": "user", "content": "prior"},)
    command = ChatCommand(content="hello", tier="code", system="system", history=history,
                          temperature=0.0, num_predict=10, num_ctx=1024,
                          session_id=SessionId.new(), turn_id=TurnId.new())

    class InspectingGateway(_Gateway):
        def generate(self, request, received_context):
            assert received_context is context
            assert request.prompt == "hello"
            assert request.tier == "code"
            assert request.system == "system"
            assert request.history == history
            assert request.options == {"temperature": 0.0, "num_predict": 10, "num_ctx": 1024}
            return super().generate(request, received_context)

    service = ChatService(InspectingGateway(), session_capture_factory=lambda: capture)
    first = service.complete(command, context)
    second = service.complete(command, context)
    assert first.capture.session_id == second.capture.session_id == command.session_id.serialize()
    assert first.capture.turn_id == second.capture.turn_id == command.turn_id.serialize()
    assert first.capture.appended[0].payload["request_id"] != second.capture.appended[0].payload["request_id"]
    assert len(repository.read_range(command.session_id.serialize())) == 6


def test_explicitly_unconfigured_capture_still_returns_response():
    gateway = _CountingGateway()
    result = ChatService(gateway).complete(
        ChatCommand(content="hello"),
        local_owner_context(correlation_id="no-capture", source="test"),
    )
    assert result.response_text == "answer:hello"
    assert result.capture is None
    assert gateway.calls == 1
