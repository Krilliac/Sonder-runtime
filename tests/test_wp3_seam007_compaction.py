"""WP3-SEAM-007 immutable CompactionEngine contract tests."""

from dataclasses import FrozenInstanceError

import pytest

from sonder_runtime.application.ports.compaction import (
    CompactionEngine,
    CompactionEvent,
    CompactionRequest,
    CompactionResult,
    CompactionSummary,
    CompactionValidation,
    SessionHistoryEvent,
    SourceRange,
    validate_compaction_result,
)


def _history():
    return tuple(
        SessionHistoryEvent(f"event-{n}", "session-1", n, "message.received", {"text": f"m{n}"})
        for n in range(1, 4)
    )


def _request():
    history = _history()
    return CompactionRequest("session-1", history, SourceRange("session-1", 1, 2, "event-1", "event-2"))


def test_request_copies_history_and_freezes_nested_payloads():
    payload = {"nested": ["original"]}
    event = SessionHistoryEvent("e", "s", 1, "message.received", payload)
    payload["nested"].append("changed")
    assert event.payload["nested"] == ("original",)
    assert isinstance(event.payload, type(event.payload))

    request = CompactionRequest("s", [event], SourceRange("s", 1, 1, "e", "e"))
    assert isinstance(request.history, tuple)
    with pytest.raises((FrozenInstanceError, AttributeError)):
        request.session_id = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SourceRange("s", 2, 1, "a", "b"),
        lambda: CompactionRequest("s", _history(), SourceRange("s", 1, 3, "event-1", "event-3")),
        lambda: CompactionRequest("s", _history(), SourceRange("other", 1, 2, "event-1", "event-2")),
    ],
)
def test_invalid_ranges_and_sessions_fail_closed(factory):
    with pytest.raises((ValueError, TypeError)):
        factory()


def test_result_binds_exact_range_and_is_append_only():
    request = _request()
    summary = CompactionSummary(facts=("fact",), confidence=0.9)
    event = CompactionEvent("compaction-1", "session-1", request.source_range, summary)
    result = CompactionResult(
        "session-1", request.source_range, summary, event, CompactionValidation(True, ("fact",))
    )

    assert validate_compaction_result(request, result).valid
    assert result.appended_event.event_type == "compaction.completed"
    assert tuple(item.event_id for item in request.history) == ("event-1", "event-2", "event-3")


def test_result_cannot_claim_a_different_source_or_reuse_history_identity():
    request = _request()
    summary = CompactionSummary()
    event = CompactionEvent("event-3", "session-1", request.source_range, summary)
    result = CompactionResult("session-1", request.source_range, summary, event, CompactionValidation(True))
    with pytest.raises(ValueError):
        validate_compaction_result(request, result)


def test_protocol_requires_compact_and_validate_without_wiring_an_adapter():
    assert {"compact", "validate"} <= set(vars(CompactionEngine))
