"""An HTTP chat turn that fails after provider admission closes its request.

Admission writes ``model.requested``; only ``model.response`` or
``model.failed`` closes it.  A turn that failed after admission (a 504 to the
client) used to leave the request open, so session repair reported the
session truncated at that turn and discarded every later, successful turn.
A legacy ``ERROR ...`` answer is likewise a failed model call, not a model
response to replay as assistant history.
"""
import types
import urllib.error

import pytest

import server
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session.provider_attempts import dispatch_provider
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.interfaces.http import serve


@pytest.fixture
def repository(tmp_path, monkeypatch):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    monkeypatch.setattr(
        bootstrap_app, "default_app",
        lambda: types.SimpleNamespace(session_capture_service=lambda: capture),
    )
    return repository


def _types(repository, session="http-session"):
    return [event.event_type for event in repository.read_range(session)]


def test_model_error_after_admission_records_model_failed(repository, monkeypatch):
    def answer(*args, **kwargs):
        try:
            dispatch_provider(
                "ollama", "/api/chat", {"model": "fixture"},
                lambda: (_ for _ in ()).throw(urllib.error.URLError("refused")),
            )
        except urllib.error.URLError as error:
            raise server.ModelCallError("timeout", "model timed out") from error

    monkeypatch.setattr(serve.server, "answer_with_history", answer)
    with pytest.raises(server.ModelCallError):
        serve._run_prompt(
            "hello", session="http-session", return_result=True,
            capture_request_id="http-request", capture_turn_id="http-turn",
        )
    events = repository.read_range("http-session")
    assert [event.event_type for event in events] == [
        "model.requested", "user.message", "provider.requested",
        "provider.failed", "model.failed",
    ]
    assert events[-1].payload["request_id"] == "http-request"
    assert events[-1].payload["error_code"] == "DEADLINE_EXCEEDED"


def test_error_before_admission_writes_nothing(repository, monkeypatch):
    def answer(*args, **kwargs):
        raise server.ModelCallError("timeout", "model timed out")

    monkeypatch.setattr(serve.server, "answer_with_history", answer)
    with pytest.raises(server.ModelCallError):
        serve._run_prompt(
            "hello", session="http-session", return_result=True,
            capture_request_id="http-request", capture_turn_id="http-turn",
        )
    assert _types(repository) == []


def test_legacy_error_answer_without_admission_is_not_captured(repository):
    serve._capture_live_session_turn(
        session_id="http-session", prompt="hello", history=[], model="fixture",
        content="ERROR: `sonder:latest` Ollama alias not found.",
        request_id="http-request", turn_id="http-turn", stream=False,
    )
    assert _types(repository) == []


def test_legacy_error_answer_after_admission_is_a_model_failure(repository, monkeypatch):
    def answer(*args, **kwargs):
        dispatch_provider("ollama", "/api/chat", {"model": "fixture"}, lambda: "x")
        return "ERROR contacting Ollama: connection refused"

    monkeypatch.setattr(serve.server, "answer_with_history", answer)
    turn = serve._run_prompt(
        "hello", session="http-session", return_result=True,
        capture_request_id="http-request", capture_turn_id="http-turn",
    )
    serve._capture_live_session_turn(
        session_id="http-session", prompt="hello", history=[], model="fixture",
        content=turn.content, request_id="http-request", turn_id="http-turn",
        stream=False, provider_capture=turn.provider_capture,
    )
    types_ = _types(repository)
    assert "model.response" not in types_
    assert types_[-1] == "model.failed"
