"""WP2 SESSION-003: durable event vocabulary and payload contract."""
from __future__ import annotations

import json

import pytest

from sonder_runtime.domain.common.events import (
    DurableEvent,
    EventKind,
    EventValidationError,
    validate_payload,
)


def test_vocabulary_covers_all_session_event_families():
    values = {kind.value for kind in EventKind}
    for family in (
        "session", "message", "context", "prompt", "model", "tool", "approval",
        "goal", "plan", "compaction", "retrieval", "subagent", "cancellation",
        "error", "artifact",
    ):
        assert any(value.startswith(family + ".") for value in values)


def test_valid_event_round_trips_as_canonical_json():
    event = DurableEvent(
        kind=EventKind.MODEL_COMPLETED,
        aggregate_type="session",
        aggregate_id="session-1",
        sequence=3,
        session_id="session-1",
        payload={"request_id": "request-1", "model": "local-code", "output_tokens": 12},
    )
    restored = DurableEvent.from_dict(json.loads(event.to_json()))
    assert restored == event
    assert restored.event_type == "model.completed"
    assert restored.id == event.event_id


@pytest.mark.parametrize(
    "kind,payload",
    [
        (EventKind.MESSAGE_RECEIVED, {"message_id": "m", "role": "user"}),
        (EventKind.TOOL_COMPLETED, {"call_id": "c", "tool": "search", "duration_ms": 4.5}),
        (EventKind.COMPACTION_COMPLETED, {"compaction_id": "x", "summary_ref": "ref:x"}),
        (EventKind.ARTIFACT_ATTACHED, {"artifact_id": "a", "sha256": "abc"}),
    ],
)
def test_payload_schemas_accept_typed_minimal_payloads(kind, payload):
    assert validate_payload(kind, payload) == payload


@pytest.mark.parametrize(
    "kind,payload",
    [
        (EventKind.MODEL_COMPLETED, {"request_id": "r"}),
        (EventKind.MESSAGE_RECEIVED, {"message_id": "m", "role": "user", "extra": 1}),
        (EventKind.TOOL_COMPLETED, {"call_id": "c", "tool": "x", "duration_ms": "slow"}),
        (EventKind.ERROR_RAISED, {"error_code": "E", "retryable": 1}),
    ],
)
def test_payload_validation_fails_closed(kind, payload):
    with pytest.raises(EventValidationError):
        validate_payload(kind, payload)


def test_unknown_kind_and_non_json_payload_are_rejected():
    with pytest.raises(EventValidationError):
        validate_payload("future.kind", {})
    with pytest.raises(EventValidationError):
        validate_payload(EventKind.CONTEXT_UPDATED, {"context_ref": object()})


def test_durable_event_rejects_invalid_envelope_values():
    with pytest.raises(EventValidationError):
        DurableEvent(EventKind.SESSION_STARTED, "session", "s", 0, {"reason": "start"}, occurred_at="yesterday")
    with pytest.raises(EventValidationError):
        DurableEvent(EventKind.SESSION_STARTED, "session", "s", -1)
