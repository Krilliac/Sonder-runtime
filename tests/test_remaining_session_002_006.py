from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session import CapturedTool, SessionCaptureService
from sonder_runtime.domain.common.errors import IntegrityFailure, InvalidInput


def _request() -> ModelRequest:
    return ModelRequest(
        prompt="Use the captured facts",
        tier="code",
        system="be exact",
        history=({"role": "user", "content": "prior"},),
        options={"temperature": 0},
        stream=False,
    )


def test_capture_integrates_append_replay_and_query_export(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    service = SessionCaptureService(repository, replay_limit=100)
    result = service.capture_turn(
        "session-1", "turn-1", _request(), request_id="request-1",
        ui_facts={"surface": "workbench", "visible_artifact": "artifact-1"},
        user_message="What happened?",
        tools=(CapturedTool("call-1", "lookup", {"key": "value"}, {"answer": 42}),),
        model_response="The answer is 42.",
    )

    assert [event.sequence for event in result.appended] == list(range(1, 6))
    assert result.replay.crash_safe
    assert result.replay.request is not None
    assert result.replay.request.request.prompt == "Use the captured facts"
    assert result.replay.request.ui_facts["surface"] == "workbench"
    assert result.replay.replay.transcript[-1].content == "The answer is 42."
    assert result.replay.replay.projection.tool_result_count == 1
    assert result.export.integrity is not None and result.export.integrity.valid
    assert not result.export.truncated
    assert [record.sequence for record in result.export.events] == [1, 2, 3, 4, 5]
    assert result.export.transcript[2].content == json.dumps(
        {"answer": 42}, sort_keys=True, separators=(",", ":"),
    )


def test_replay_and_export_are_repeatable_for_the_same_durable_stream(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    service = SessionCaptureService(repository, replay_limit=100)
    first = service.capture_turn("s", "t", _request(), request_id="r", model_response="ok")
    second = service.capture_turn("s", "t2", _request(), request_id="r2", model_response="again")

    from sonder_runtime.application.session.durable_replay import crash_safe_replay

    replay_a = crash_safe_replay(repository, "s", max_events=100)
    replay_b = crash_safe_replay(repository, "s", max_events=100)
    assert replay_a.replay == replay_b.replay
    assert service._query.export_events("s", max_events=100).to_jsonl() == service._query.export_events("s", max_events=100).to_jsonl()
    assert [item.content for item in second.export.transcript] == ["ok", "again"]


def test_capture_rejects_non_json_tool_facts_before_writing(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    service = SessionCaptureService(repository, replay_limit=100)
    with pytest.raises(InvalidInput, match="JSON"):
        service.capture_turn(
            "s", "t", _request(), request_id="r",
            tools=(CapturedTool("c", "bad", {}, object()),),
        )
    assert repository.read_range("s", limit=100) == ()


def test_capture_fails_closed_when_repository_integrity_is_not_proven(tmp_path) -> None:
    repository = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    service = SessionCaptureService(repository, replay_limit=100)
    result = service.capture_turn("s", "t", _request(), request_id="r")
    assert result.replay.integrity.valid

    class CorruptRepository:
        _max_read_limit = 100

        def append(self, *args, **kwargs):
            return repository.append(*args, **kwargs)

        def read_range(self, *args, **kwargs):
            return repository.read_range(*args, **kwargs)

        def inspect_integrity(self, session_id, **kwargs):
            report = repository.inspect_integrity(session_id, **kwargs)
            from dataclasses import replace
            return replace(report, valid=False)

    with pytest.raises(IntegrityFailure):
        SessionCaptureService(CorruptRepository(), replay_limit=100).capture_turn(
            "s", "t2", _request(), request_id="r2",
        )
