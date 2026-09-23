from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.context_manifests import (
    ContextRecord, PrefixCacheObservation, build_prefix_manifest,
    build_replay_manifest,
)
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


def test_capture_persists_prefix_and_replay_evidence_without_prompt_contents(tmp_path):
    record = ContextRecord(
        "rule-1", "project_rules", "keep changes bounded", "project", 1, True,
    )
    prefix = build_prefix_manifest((record,), model="model", provider_id="ollama")
    replay = build_replay_manifest(
        "r1", "model", (record,), prefix_key=prefix.cache_key,
        metadata={"producer": "live-agent-context"},
    )
    request = ModelRequest(
        prompt="hello", tier="code", prefix_manifest=prefix,
        replay_manifest=replay,
        prefix_cache_observation=PrefixCacheObservation(
            prefix.cache_key, prefix.identity_key, prefix.version,
            "miss", "cold_start", True,
        ),
    )
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    result = SessionCaptureService(repository).capture_turn(
        "s1", "t1", request, request_id="r1", model_response="world",
    )
    payload = result.appended[0].payload
    assert payload["prefix_manifest"]["cache_key"] == prefix.cache_key
    assert payload["replay_manifest"]["manifest_digest"] == replay.manifest_digest
    assert payload["replay_manifest"]["sections"][0]["content_digest"] == record.content_digest
    assert "keep changes bounded" not in str(payload)
    reconstructed = result.replay.replay.request
    assert reconstructed is not None
    assert reconstructed.replay_manifest["manifest_digest"] == replay.manifest_digest
    assert reconstructed.prefix_cache_observation["reason"] == "cold_start"


def test_request_rejects_cache_observation_for_another_prefix():
    first = build_prefix_manifest(
        (ContextRecord("first", "project_rules", "one", "project", 1, True),),
        model="model", provider_id="ollama",
    )
    second = build_prefix_manifest(
        (ContextRecord("second", "project_rules", "two", "project", 1, True),),
        model="model", provider_id="ollama",
    )
    with pytest.raises(ValueError, match="does not match"):
        ModelRequest(
            prompt="hello", tier="code", prefix_manifest=first,
            prefix_cache_observation=PrefixCacheObservation(
                second.cache_key, second.identity_key, second.version,
                "hit", "hit", False,
            ),
        )
    with pytest.raises(ValueError, match="replay manifest"):
        ModelRequest(
            prompt="hello", tier="code", prefix_manifest=first,
            replay_manifest=build_replay_manifest(
                "r2", "model", (), prefix_key=second.cache_key,
            ),
        )

    forged = ModelRequest(
        prompt="hello", tier="code",
        prefix_manifest=SimpleNamespace(
            cache_key=first.cache_key, identity_key=first.identity_key,
            version=first.version, sections=first.sections,
        ),
        prefix_cache_observation=PrefixCacheObservation(
            first.cache_key, first.identity_key, first.version,
            "hit", "hit", False,
        ),
    )
    with pytest.raises(InvalidInput, match="live immutable manifest"):
        SessionCaptureService(SQLiteSessionRepository(":memory:")).capture_turn(
            "s1", "t1", forged, request_id="r1", model_response="world",
        )
