from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.compaction import SessionCompactionError, SessionCompactionService


def test_compaction_reads_durable_history_and_appends_typed_summary(tmp_path):
    path = tmp_path / "sessions.db"
    repo = SQLiteSessionRepository(path)
    repo.append("s1", "message.received", {"facts": ["local-first"]}, event_id="e1")
    repo.append("s1", "tool.completed", {
        "artifacts": ["artifact-1"], "modality": "tool",
        "payload": {"ok": True},
    }, event_id="e2")

    event = SessionCompactionService(repo, event_id_factory=lambda: "c1").compact(
        "s1", start_sequence=1, end_sequence=2,
    )

    assert event.event_id == "c1"
    assert event.event_type == "compaction.completed"
    assert event.payload["source_range"]["end_event_id"] == "e2"
    assert event.payload["summary"]["facts"] == ["local-first"]
    assert event.payload["summary"]["modalities"][0]["modality"] == "tool"
    assert tuple(item.event_id for item in repo.read_range("s1", limit=2)) == ("e1", "e2")

    reopened = SQLiteSessionRepository(path)
    assert reopened.read_range("s1", start_sequence=3, limit=1)[0].event_id == "c1"


def test_compaction_rejects_truncated_range_without_side_effect(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append("s1", "message.received", {"facts": ["x"]}, event_id="e1")

    with pytest.raises(SessionCompactionError, match="truncated"):
        SessionCompactionService(repo).compact("s1", start_sequence=1, end_sequence=2)
    assert len(repo.read_range("s1", limit=10)) == 1


def test_application_graph_exposes_one_compaction_service(tmp_path, monkeypatch):
    from sonder_runtime.bootstrap import app

    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    app.reset_for_tests()
    application = app.default_app()
    assert application.compaction_service is not None
    assert application.compaction_service() is application.compaction_service()


def test_canonical_bootstrap_compaction_preserves_structured_payload_after_restart(
    tmp_path, monkeypatch
):
    from sonder_runtime.bootstrap import app

    database = tmp_path / "sessions.db"
    monkeypatch.setenv("SONDER_SESSIONS_DB", str(database))
    first = app.build_application()
    repository = first.session_repository()
    repository.append(
        "s1",
        "message.received",
        {
            "facts": ["local-first"],
            "decisions": ["use sqlite"],
            "unresolved_tasks": ["verify restart"],
            "confidence": 0.95,
            "modality": "text",
        },
        event_id="e1",
    )
    repository.append(
        "s1",
        "tool.completed",
        {
            "artifacts": ["report.json"],
            "tool_outcomes": ["ok"],
            "confidence": 0.8,
            "modality": "tool",
            "payload": {"ok": True},
        },
        event_id="e2",
    )
    repository.append(
        "s1",
        "attachment.added",
        {"artifacts": ["image.png"], "modality": "image"},
        event_id="e3",
    )

    compacted = first.compaction_service().compact(
        "s1", start_sequence=1, end_sequence=3,
    )

    assert compacted.sequence == 4
    assert compacted.payload["source_range"] == {
        "session_id": "s1",
        "start_sequence": 1,
        "end_sequence": 3,
        "start_event_id": "e1",
        "end_event_id": "e3",
    }
    summary = compacted.payload["summary"]
    assert summary["facts"] == ["local-first"]
    assert summary["decisions"] == ["use sqlite"]
    assert summary["unresolved_tasks"] == ["verify restart"]
    assert summary["artifacts"] == ["report.json", "image.png"]
    assert summary["tool_outcomes"] == ["ok"]
    assert summary["confidence"] == 0.8
    assert [item["modality"] for item in summary["modalities"]] == ["tool", "image"]
    assert [item["event_id"] for item in summary["modalities"]] == ["e2", "e3"]

    recompacted = first.compaction_service().compact(
        "s1", start_sequence=1, end_sequence=3,
    )
    assert recompacted.sequence == 5
    assert recompacted.payload["source_range"] == compacted.payload["source_range"]
    assert [event.event_id for event in repository.read_range("s1", limit=3)] == [
        "e1", "e2", "e3",
    ]

    # Reopen through the canonical composition root: the append and its
    # source range must remain visible without process-local state.
    reopened = app.build_application()
    replayed = reopened.session_repository().read_range("s1", start_sequence=4, limit=2)
    assert replayed[0].event_id == compacted.event_id
    assert replayed[0].payload == compacted.payload
    assert replayed[1].event_id == recompacted.event_id
    assert replayed[1].payload == recompacted.payload
    assert reopened.session_repository().inspect_integrity("s1").valid


def test_invalid_confidence_fails_closed_without_appending(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append("s1", "message.received", {"facts": ["x"], "confidence": 1.5}, event_id="e1")

    with pytest.raises(SessionCompactionError, match="confidence"):
        SessionCompactionService(repo).compact("s1", start_sequence=1, end_sequence=1)
    assert [event.event_id for event in repo.read_range("s1", limit=10)] == ["e1"]
