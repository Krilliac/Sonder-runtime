from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.compaction.append_service import (
    CompactionAppendError,
    CompactionAppendService,
    ImmutableSourceEventRange,
    StructuredCompaction,
)


def _source(repo):
    first = repo.append("s1", "user.message", {"content": "hello"}, event_id="e1")
    second = repo.append("s1", "model.response", {"content": "world"}, event_id="e2")
    return ImmutableSourceEventRange("s1", 1, 2, first.event_id, second.event_id)


def test_append_preserves_source_range_and_records_structured_compaction(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    source = _source(repo)
    event = CompactionAppendService(repo, event_id_factory=lambda: "compact-1").append(
        source,
        StructuredCompaction(
            facts=("greeting",), decisions=("answer plainly",),
            unresolved_tasks=("none",), artifacts=("artifact-1",),
        ),
    )

    assert event.event_type == "compaction.completed"
    assert event.sequence == 3
    assert tuple(item.event_id for item in repo.read_range("s1", limit=2)) == ("e1", "e2")
    assert event.payload["source_range"]["start_event_id"] == "e1"
    assert event.payload["summary"]["facts"] == ["greeting"]


def test_append_rejects_truncated_or_mismatched_source_without_writing(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    source = _source(repo)
    service = CompactionAppendService(repo)
    bad = ImmutableSourceEventRange("s1", 1, 2, source.start_event_id, "wrong")
    with pytest.raises(CompactionAppendError, match="identities"):
        service.append(bad, StructuredCompaction(facts=("x",)))
    assert len(repo.read_range("s1", limit=10)) == 2


def test_structured_compaction_has_item_bounds(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    source = _source(repo)
    with pytest.raises(CompactionAppendError, match="facts"):
        CompactionAppendService(repo).append(
            source, StructuredCompaction(facts=("a", "b"), max_items=1)
        )
