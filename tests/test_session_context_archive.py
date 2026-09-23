"""Bounded context archive canaries for issue #510 item 1."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.archive import SessionContextArchiveService
from sonder_runtime.application.compaction import SessionCompactionService


def _history(repo):
    request = repo.append(
        "s1", "model.requested", {"request_id": "r1", "decision": "keep contract"}, event_id="request"
    )
    tool_small = repo.append(
        "s1", "tool.result", {"call_id": "small", "content": "minor output"}, event_id="small"
    )
    tool_large = repo.append(
        "s1", "tool.result", {"call_id": "large", "content": "secret output " * 80}, event_id="large"
    )
    failed = repo.append(
        "s1", "model.failed", {"request_id": "r1", "error_code": "timeout"}, event_id="failed"
    )
    return request, tool_small, tool_large, failed


def test_selective_eviction_archives_largest_tool_output_and_preserves_failures(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    request, tool_small, tool_large, failed = _history(repo)
    service = SessionContextArchiveService(repo, event_id_factory=lambda: "archive-1")

    result = service.prepare_context(
        "s1", (request, tool_small, tool_large, failed),
        budget_bytes=400,
    )

    assert result.evicted_event_ids == ("large",)
    assert [event.event_id for event in result.retained_events] == ["request", "small", "failed"]
    assert result.placeholders[0]["archive_id"] == "archive-1"
    archive_event = repo.search(session_id="s1", event_type="context.archive.created", limit=1)[0]
    assert "secret output" not in str(archive_event.payload)
    assert archive_event.payload["source_event_id"] == "large"


def test_archive_retrieval_survives_restart_and_raw_failure_history_remains_searchable(tmp_path):
    database = tmp_path / "sessions.db"
    first = SQLiteSessionRepository(database)
    _, _, tool_large, failed = _history(first)
    service = SessionContextArchiveService(first)
    result = service.prepare_context("s1", tuple(first.read_range("s1", limit=10)), budget_bytes=1)
    assert result.evicted_event_ids == ("large",)

    reopened = SQLiteSessionRepository(database)
    recovered = SessionContextArchiveService(reopened).retrieve(result.references[0])
    assert recovered["content"].startswith("secret output")
    assert reopened.inspect_integrity("s1").valid
    matches = SessionContextArchiveService(reopened).search("s1", "timeout")
    assert any(event.event_id == failed.event_id for event in matches)


def test_compaction_service_exposes_the_durable_archive_seam(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    _history(repo)
    result = SessionCompactionService(repo).archive_context("s1", budget_bytes=1)
    assert result.evicted_event_ids == ("large",)


def test_placeholder_overhead_is_counted_and_reports_remaining_overflow(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    request, _, tool_large, failed = _history(repo)
    service = SessionContextArchiveService(repo, event_id_factory=lambda: "archive-1")
    raw_budget = sum(len(str(event.payload)) for event in (request, failed))
    result = service.prepare_context(
        "s1", (request, tool_large, failed), budget_bytes=raw_budget,
    )

    assert result.used_bytes > result.budget_bytes
    assert result.overflow is True
    assert result.needs_compaction is True


def test_protected_history_is_never_evicted_and_explicitly_reports_overflow(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    request, _, _, failed = _history(repo)
    service = SessionContextArchiveService(repo)
    result = service.prepare_context("s1", (request, failed), budget_bytes=0)

    assert result.references == ()
    assert result.evicted_event_ids == ()
    assert result.used_bytes > 0
    assert result.needs_compaction is True


def test_tiny_tool_result_is_not_replaced_by_a_larger_placeholder(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    request = repo.append("s1", "model.requested", {"request_id": "r1"}, event_id="request")
    tiny = repo.append("s1", "tool.result", {"x": 1}, event_id="tiny")
    service = SessionContextArchiveService(repo, event_id_factory=lambda: "archive-id")

    result = service.prepare_context("s1", (request, tiny), budget_bytes=0)

    assert result.references == ()
    assert result.retained_events == (request, tiny)
    assert result.needs_compaction is True
    assert repo.search(session_id="s1", event_type="context.archive.created") == ()


def test_open_ended_archive_range_fails_closed_when_history_exceeds_bound(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append("s1", "model.requested", {"request_id": "r1"}, event_id="request")
    repo.append("s1", "model.failed", {"error_code": "timeout"}, event_id="failed")
    repo.append("s1", "goal.updated", {"decision": "retain"}, event_id="decision")

    from sonder_runtime.application.compaction import SessionCompactionError, SessionCompactionService

    with pytest.raises(SessionCompactionError, match="exceeds"):
        SessionCompactionService(repo, max_events=2).archive_context("s1", budget_bytes=1)
    assert repo.search(session_id="s1", event_type="context.archive.created") == ()
