"""Bounded context archive canaries for issue #510 item 1."""
from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.archive import SessionContextArchiveService
from sonder_runtime.application.compaction import SessionCompactionService
from sonder_runtime.domain.common.errors import IntegrityFailure, InvalidInput


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


def test_archiving_same_source_event_is_idempotent_across_repeated_requests(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    event = repo.append("s1", "tool.result", {"content": "x" * 500}, event_id="tool")
    first = SessionContextArchiveService(repo).archive_tool_output(event)
    second = SessionContextArchiveService(repo).archive_tool_output(event)

    assert second == first
    assert len(repo.search(session_id="s1", event_type="context.archive.created", limit=10)) == 1


def test_repeated_prepare_binds_placeholder_to_persisted_reference(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    event = repo.append(
        "s1", "tool.result", {"content": "x" * 500}, event_id="tool"
    )
    service = SessionContextArchiveService(repo)
    first = service.prepare_context("s1", (event,), budget_bytes=1)
    second = service.prepare_context("s1", (event,), budget_bytes=1)
    persisted = repo.search(
        session_id="s1", event_type="context.archive.created", limit=10
    )

    assert len(persisted) == 1
    assert first.references == second.references
    assert first.placeholders[0]["archive_id"] == persisted[0].payload["archive_id"]
    assert second.placeholders[0]["archive_id"] == persisted[0].payload["archive_id"]
    assert persisted[0].payload["archive_id"] in second.placeholders[0]["content"]


def test_external_lane_reference_reuses_id_and_verifies_source_payload(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    service = SessionContextArchiveService(repo, event_id_factory=lambda: "archive-lane")
    source = {
        "event_id": "lane-event-1", "sequence": 7, "event_type": "tool.result",
        "payload": {"output": "lane output", "project_id": "project-root"},
    }
    first = service.archive_external_tool_output(
        session_id="s1", project_id="project-root", source_kind="agent_lane",
        source_lane_id="lane-1", source_event_id=source["event_id"],
        source_sequence=source["sequence"], source_payload=source["payload"],
    )
    second = service.archive_external_tool_output(
        session_id="s1", project_id="project-root", source_kind="agent_lane",
        source_lane_id="lane-1", source_event_id=source["event_id"],
        source_sequence=source["sequence"], source_payload=source["payload"],
    )

    assert second == first
    assert SessionContextArchiveService.retrieve_external(
        first, lambda lane, sequence: source, project_id="project-root"
    ) == source["payload"]
    assert len(repo.search(session_id="s1", event_type="context.archive.created", limit=10)) == 1
    with pytest.raises(IntegrityFailure, match="unavailable"):
        SessionContextArchiveService.retrieve_external(
            first, lambda lane, sequence: None, project_id="project-root"
        )
    with pytest.raises(IntegrityFailure, match="project scope"):
        SessionContextArchiveService.retrieve_external(
            first, lambda lane, sequence: source, project_id="other-project"
        )


def test_archive_rejects_unbounded_event_source_after_one_extra_item(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    event = repo.append("s1", "tool.result", {"content": "x"}, event_id="source")
    service = SessionContextArchiveService(repo, max_items=2)
    consumed = 0

    def source():
        nonlocal consumed
        while True:
            consumed += 1
            yield event

    with pytest.raises(InvalidInput, match="event count exceeds"):
        service.prepare_context("s1", source(), budget_bytes=1)
    assert consumed == 3


def test_session_tail_reads_recent_events_with_a_small_adapter_limit(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=2)
    for index in range(4):
        repo.append("s1", "model.response", {"content": str(index)}, event_id=f"e{index}")

    assert [event.event_id for event in repo.read_tail("s1", limit=2)] == ["e2", "e3"]
    assert [event.event_id for event in repo.read_range("s1", limit=2)] == ["e0", "e1"]
