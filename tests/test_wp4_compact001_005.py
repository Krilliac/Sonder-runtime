"""WP4 COMPACT-001-005 application compaction tests."""

from dataclasses import FrozenInstanceError

import pytest

from sonder_runtime.application.compaction import CompactionApplicationService
from sonder_runtime.application.ports.compaction import (
    CompactionRequest, CompactionValidationError, SessionHistoryEvent, SourceRange,
)


def _request():
    history = (
        SessionHistoryEvent("e1", "s", 1, "message.received", {"facts": ["alpha"], "text": "hello"}),
        SessionHistoryEvent("e2", "s", 2, "tool.completed", {"tool_outcomes": ["ok"], "facts": ["beta"]}, "tool"),
        SessionHistoryEvent("e3", "s", 3, "attachment.added", {"artifacts": ["report.pdf"]}, "attachment"),
    )
    return CompactionRequest("s", history, SourceRange("s", 1, 3, "e1", "e3"))


def test_compaction_is_append_only_and_binds_exact_source_range():
    request = _request()
    result = CompactionApplicationService(event_id_factory=lambda: "c1").compact(request)
    assert result.appended_event.event_id == "c1"
    assert result.appended_event.source_range == request.source_range
    assert tuple(event.event_id for event in request.history) == ("e1", "e2", "e3")
    with pytest.raises((FrozenInstanceError, AttributeError)):
        request.history = ()  # type: ignore[misc]


def test_structured_retention_and_typed_modalities_are_separate():
    result = CompactionApplicationService(event_id_factory=lambda: "c1").compact(_request())
    assert result.summary.facts == ("alpha", "beta")
    assert result.summary.tool_outcomes == ("ok",)
    assert result.summary.artifacts == ("report.pdf",)
    assert tuple(event.modality for event in result.summary.modalities) == ("tool", "attachment")
    assert result.summary.modalities[0].payload["facts"] == ("beta",)


def test_validation_reports_missing_source_facts_and_recompaction_uses_original_history():
    request = _request()
    service = CompactionApplicationService(event_id_factory=iter(("c1", "c2")).__next__)
    first = service.compact(request)
    assert first.validation.valid
    altered = first.summary.__class__(facts=("alpha",), modalities=first.summary.modalities)
    altered_event = first.appended_event.__class__("c-bad", "s", request.source_range, altered)
    bad = first.__class__("s", request.source_range, altered, altered_event, first.validation)
    checked = service.validate(request, bad)
    assert not checked.valid
    assert checked.missing_facts == ("beta",)
    second = service.compact(request)
    assert second.appended_event.event_id == "c2"
    assert second.validation.valid


def test_unknown_modalities_are_preserved_and_budget_fails_closed():
    event = SessionHistoryEvent("e", "s", 1, "image.added", {"text": "x"}, "video")
    request = CompactionRequest("s", (event,), SourceRange("s", 1, 1, "e", "e"))
    result = CompactionApplicationService().compact(request)
    assert result.summary.modalities[0].modality == "video"
    budgeted = CompactionRequest(
        "s", _request().history, _request().source_range, max_summary_tokens=1,
    )
    with pytest.raises(CompactionValidationError):
        CompactionApplicationService().compact(budgeted)
