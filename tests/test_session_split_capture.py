from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.domain.common.errors import InvalidInput


def test_begin_is_committed_and_success_is_correlated(tmp_path):
    database = tmp_path / "session.db"
    repository = SQLiteSessionRepository(database)
    capture = SessionCaptureService(repository)
    pending = capture.begin_request(
        "s1", "t1", ModelRequest(prompt="hello", tier="code"),
        request_id="r1", user_message="hello",
    )
    reopened = SQLiteSessionRepository(database)
    events = reopened.read_range("s1")
    assert [event.event_type for event in events] == ["model.requested", "user.message"]
    assert pending.appended == events
    assert pending.session_id == "s1"
    assert pending.turn_id == "t1"
    assert pending.request_id == "r1"
    assert all(event.payload["request_id"] == "r1" for event in events)
    assert len(events[0].payload["snapshot_digest"]) == 64
    assert events[0].payload["tools"] == []
    assert events[0].payload["ui_facts"] == {}
    completed = capture.complete_request(pending, model_response="world")
    assert completed.appended[-1].payload == {
        "content": "world", "turn_id": "t1", "request_id": "r1",
    }
    assert len(reopened.read_range("s1")) == 3
    assert completed.replay.replay.transcript[-1].content == "world"
    assert completed.export.integrity.valid


def test_failure_appends_only_identity_and_stable_code(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    pending = capture.begin_request(
        "s1", "t1", ModelRequest(prompt="hello", tier="code"), request_id="r1",
    )
    failed = capture.fail_request(pending, error_code="CANCELLED")
    assert failed.event_type == "model.failed"
    assert failed.payload == {"turn_id": "t1", "request_id": "r1", "error_code": "CANCELLED"}
    assert [event.event_type for event in repository.read_range("s1")] == [
        "model.requested", "model.failed",
    ]


@pytest.mark.parametrize("override", [
    {"session_id": " "}, {"turn_id": ""}, {"request_id": None},
    {"user_message": " "}, {"user_message": 3}, {"request": object()},
    {"request": ModelRequest(prompt="", tier="code")},
    {"request": ModelRequest(prompt="hello", tier="")},
    {"request": ModelRequest(prompt="hello", tier="code", options={"bad": object()})},
])
def test_invalid_admission_appends_nothing(tmp_path, override):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    arguments = dict(session_id="s1", turn_id="t1", request_id="r1", user_message="hello",
                     request=ModelRequest(prompt="hello", tier="code"))
    arguments.update(override)
    with pytest.raises(InvalidInput):
        capture.begin_request(**arguments)
    assert repository.read_range("s1") == ()


@pytest.mark.parametrize("code", ["UNKNOWN", "private exception text", "", None, []])
def test_invalid_failure_code_does_not_append(tmp_path, code):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    pending = capture.begin_request(
        "s1", "t1", ModelRequest(prompt="hello", tier="code"), request_id="r1",
    )
    with pytest.raises(InvalidInput):
        capture.fail_request(pending, error_code=code)
    assert len(repository.read_range("s1")) == 1


def test_invalid_response_does_not_append(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    pending = capture.begin_request(
        "s1", "t1", ModelRequest(prompt="hello", tier="code"), request_id="r1",
    )
    with pytest.raises(InvalidInput):
        capture.complete_request(pending, model_response=" ")
    assert len(repository.read_range("s1")) == 1


def test_retrospective_response_keeps_supplied_request_identity(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    result = SessionCaptureService(repository).capture_turn(
        "s1", "t1", ModelRequest(prompt="hello", tier="code"),
        request_id="legacy-r1", user_message="hello", model_response="world",
    )
    assert len(result.appended) == 3
    assert result.appended[-1].payload["request_id"] == "legacy-r1"
