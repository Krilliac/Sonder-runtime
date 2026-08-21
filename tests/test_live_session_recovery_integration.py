from __future__ import annotations

import pytest

import server
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.durable_replay import crash_safe_replay
from sonder_runtime.bootstrap import app as bootstrap_app


def test_live_turn_bridge_commits_and_reopens_typed_session(tmp_path, monkeypatch):
    database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    bootstrap_app.reset_for_tests()
    application = bootstrap_app.build_application()
    monkeypatch.setattr(server, "_application", lambda: application)

    captured = server._capture_durable_session_turn(
        "live-session",
        "persist this live turn",
        [{"role": "user", "content": "earlier"}],
        "local-model",
        "system boundary",
        "sonder",
        "durable answer",
        request_id="legacy-interaction-1",
    )

    assert captured is not None
    events = application.session_repository().read_range("live-session", limit=20)
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "model.response",
    ]
    assert events[0].payload["request_id"] == "legacy-interaction-1"

    bootstrap_app.reset_for_tests()
    reopened = bootstrap_app.build_application()
    replay = crash_safe_replay(
        reopened.session_repository(), "live-session", max_events=20,
    )
    assert replay.crash_safe
    assert replay.request is not None
    assert replay.request.request == ModelRequest(
        prompt="persist this live turn",
        tier="sonder",
        system="system boundary",
        history=(({"role": "user", "content": "earlier"}),),
    )
    assert replay.replay.transcript[-1].content == "durable answer"


def test_live_turn_bridge_fails_closed_when_durable_capture_fails(monkeypatch):
    class _BrokenCapture:
        def capture_turn(self, *args, **kwargs):
            raise RuntimeError("durable store unavailable")

    class _Application:
        def session_capture_service(self):
            return _BrokenCapture()

    monkeypatch.setattr(server, "_application", lambda: _Application())

    with pytest.raises(RuntimeError, match="durable store unavailable"):
        server._capture_durable_session_turn(
            "live-session", "prompt", (), "model", "system", "sonder", "answer",
        )
