"""Real process-exit evidence at the typed chat/model boundary."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.chat.handle_chat import ChatCommand
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session.continuity import SessionContinuityService
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.ids import SessionId


_CHILD = """
import os
import sys
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.domain.common.ids import SessionId

class FixtureGateway:
    def generate(self, request, context):
        if sys.argv[2] == 'crash':
            os._exit(91)
        return ModelResponse(text='fixture answer', model='fixture', tier=request.tier)

repository = SQLiteSessionRepository(sys.argv[1])
ChatService(FixtureGateway(), SessionCaptureService(repository)).complete(
    ChatCommand(content='fixture input', session_id=SessionId.new()),
    local_owner_context(correlation_id='process-fixture', source='system'),
)
"""


@pytest.mark.parametrize("outcome, exit_code", [("crash", 91), ("success", 0)])
def test_request_survives_gateway_process_exit(tmp_path, outcome, exit_code):
    database = tmp_path / "sessions.db"
    child = subprocess.run(
        [sys.executable, "-c", _CHILD, str(database), outcome],
        cwd=Path(__file__).resolve().parents[1], timeout=20,
        capture_output=True, text=True,
    )
    assert child.returncode == exit_code, child.stderr

    repository = SQLiteSessionRepository(database)
    requests = repository.search(event_type="model.requested", limit=10)
    assert len(requests) == 1
    request = requests[0]
    events = repository.read_range(request.session_id)
    expected = ["model.requested", "user.message"]
    if outcome == "success":
        expected.append("model.response")
    assert [event.event_type for event in events] == expected
    assert events[1].payload["content"] == "fixture input"
    assert all(event.payload["request_id"] == request.payload["request_id"] for event in events)
    assert repository.inspect_integrity(request.session_id).valid

    # Recovery consumes the stored stream only. Repeated diagnosis/replay must
    # neither dispatch another attempt nor append a fabricated terminal event.
    continuity = SessionContinuityService(repository)
    plan = continuity.resume(request.session_id)
    assert plan.diagnosis.disposition == ("truncated" if outcome == "crash" else "clean")
    replay = SessionCaptureService(repository).replay(request.session_id)
    assert replay.request.request.prompt == "fixture input"
    assert [item.content for item in replay.replay.transcript] == (
        ["fixture input"] if outcome == "crash" else ["fixture input", "fixture answer"]
    )
    assert continuity.resume(request.session_id) == plan
    assert repository.read_range(request.session_id) == events


def test_bootstrap_lazy_capture_commits_before_model_dispatch(tmp_path, monkeypatch):
    database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    bootstrap_app.reset_for_tests()
    try:
        graph = bootstrap_app.build_application()
        session_id = SessionId.new()
        context = local_owner_context(correlation_id="composed-capture", source="system")
        calls = []

        def inspect_request(request, actual_context):
            calls.append(request)
            assert actual_context is context
            events = SQLiteSessionRepository(database).read_range(session_id.serialize())
            assert [event.event_type for event in events] == ["model.requested", "user.message"]
            assert events[0].payload["prompt"] == request.prompt
            return ModelResponse(text="composed answer", model="fixture", tier=request.tier)

        monkeypatch.setattr(graph.model_gateway, "generate", inspect_request)
        result = graph.chat.complete(
            ChatCommand(content="composed input", session_id=session_id), context,
        )
        assert len(calls) == 1
        assert result.response_text == "composed answer"
        assert result.capture is not None
        assert result.capture.export.integrity.valid
        assert [event.event_type for event in result.capture.appended] == [
            "model.requested", "user.message", "model.response",
        ]
    finally:
        bootstrap_app.reset_for_tests()
