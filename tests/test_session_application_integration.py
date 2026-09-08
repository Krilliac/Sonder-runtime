from __future__ import annotations

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.chat.handle_chat import ChatCommand
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.session.durable_replay import crash_safe_replay
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.ids import SessionId


class _DeterministicGateway:
    def __init__(self) -> None:
        self.fail = False

    def generate(self, request, context):
        if self.fail:
            raise RuntimeError("deterministic provider failure")
        return ModelResponse(
            text=f"reply:{request.prompt}", model="integration-model", tier=request.tier,
        )


@pytest.mark.integration
def test_application_chat_persists_and_reopens_durable_session_stream(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    bootstrap_app.reset_for_tests()

    try:
        application = bootstrap_app.build_application()
        gateway = _DeterministicGateway()
        # Keep the production composition graph intact while making the model
        # boundary deterministic and local to this integration test.
        application.chat._gateway = gateway
        session_id = SessionId.new()
        context = local_owner_context(
            correlation_id="session-application-integration", source="test",
        )

        result = application.chat.complete(
            ChatCommand(content="persist this turn", session_id=session_id),
            context,
        )

        assert result.response_text == "reply:persist this turn"
        assert database.exists()
        first_events = application.session_repository().read_range(
            session_id.serialize(), limit=100,
        )
        assert [event.event_type for event in first_events] == [
            "model.requested", "user.message", "model.response",
        ]

        gateway.fail = True
        failed_input = "inspect the unavailable provider"
        with pytest.raises(RuntimeError, match="deterministic provider failure"):
            application.chat.complete(
                ChatCommand(content=failed_input, session_id=session_id),
                context,
            )
        all_events = application.session_repository().read_range(
            session_id.serialize(), limit=100,
        )
        assert all_events[:3] == first_events
        failed_events = all_events[3:]
        assert [event.event_type for event in failed_events] == [
            "model.requested", "user.message", "model.failed",
        ]
        requested, user_message, failure = failed_events
        request_id = requested.payload["request_id"]
        turn_id = requested.payload["turn_id"]
        assert request_id != first_events[0].payload["request_id"]
        assert turn_id != first_events[0].payload["turn_id"]
        assert requested.payload["prompt"] == failed_input
        assert user_message.payload == {
            "request_id": request_id, "turn_id": turn_id, "content": failed_input,
        }
        assert failure.payload == {
            "request_id": request_id, "turn_id": turn_id,
            "error_code": "INTERNAL_FAILURE",
        }
        assert all("deterministic provider failure" not in str(event.payload)
                   for event in all_events)

        bootstrap_app.reset_for_tests()
        reopened = bootstrap_app.build_application()
        repository = reopened.session_repository()
        replay = crash_safe_replay(repository, session_id.serialize(), max_events=100)

        assert replay.crash_safe
        assert replay.integrity.valid
        assert replay.recovered_sequence == 6
        assert replay.replay.projection.event_count == 6
        assert replay.replay.projection.assistant_message_count == 1
        assert replay.replay.projection.error_count == 1
        assert [(message.role, message.content) for message in replay.replay.transcript] == [
            ("user", "persist this turn"),
            ("assistant", "reply:persist this turn"),
            ("user", failed_input),
        ]
        assert repository.read_range(session_id.serialize(), limit=100) == all_events
    finally:
        bootstrap_app.reset_for_tests()
