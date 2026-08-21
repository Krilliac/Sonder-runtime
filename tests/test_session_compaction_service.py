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
