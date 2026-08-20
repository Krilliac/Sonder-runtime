from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.query_export import (
    QueryExportError, SessionEventRecord, SessionQueryEngine,
)
from sonder_runtime.application.session.replay import replay_session
from sonder_runtime.platform.logging import Redactor


def _repo(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    repo.append("s1", "user.message", {"content": "hello", "turn_id": "t1"})
    repo.append("s1", "model.response", {"content": "token=super-secret", "turn_id": "t1"})
    repo.append("s1", "tool.call", {"content": "run", "name": "shell"})
    repo.append("s1", "session.closed", {"status": "closed"})
    return repo


def test_query_is_bounded_paginated_and_cursor_is_query_bound(tmp_path):
    engine = SessionQueryEngine(_repo(tmp_path), max_page_size=2, max_scan=4)
    first = engine.query_events("s1", page_size=2)
    assert [item.sequence for item in first.records] == [1, 2]
    assert first.has_more and first.scanned == 4
    second = engine.query_events("s1", page_size=2, cursor=first.next_cursor)
    assert [item.sequence for item in second.records] == [3, 4]
    with pytest.raises(QueryExportError, match="does not match"):
        engine.query_events("s1", text="hello", page_size=2, cursor=first.next_cursor)


def test_search_filter_advances_past_nonmatching_events(tmp_path):
    engine = SessionQueryEngine(_repo(tmp_path), max_page_size=2, max_scan=10)
    page = engine.query_events("s1", event_type="tool.call", page_size=1)
    assert [item.sequence for item in page.records] == [3]
    assert not page.has_more


def test_export_redacts_recursively_and_keeps_replay_envelope(tmp_path):
    engine = SessionQueryEngine(_repo(tmp_path), redactor=Redactor(secret_values=("super-secret",)))
    exported = engine.export_events("s1", max_events=10)
    assert exported.integrity is not None and exported.integrity.valid
    assert exported.events[1].redacted
    assert "super-secret" not in exported.to_jsonl()
    replay = replay_session(tuple(item.to_domain_event() for item in exported.events))
    assert replay.session_id == "s1"
    assert replay.transcript[1].content == "token=[REDACTED]"
    assert json.loads(exported.to_jsonl().splitlines()[0])["sequence"] == 1


def test_transcript_export_supports_message_vocabulary_and_is_bounded(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "session.db", max_read_limit=100)
    repo.append("s2", "message.received", {"content": "question"})
    repo.append("s2", "message.emitted", {"content": "answer"})
    repo.append("s2", "message.received", {"content": "later"})
    engine = SessionQueryEngine(repo, max_page_size=2, max_scan=10)
    transcript = engine.export_transcript("s2", max_events=2)
    assert [(item.role, item.content) for item in transcript] == [("user", "question"), ("assistant", "answer")]
    assert engine.export_events("s2", max_events=2).truncated


def test_rejects_unbounded_limits_and_malformed_cursor(tmp_path):
    engine = SessionQueryEngine(_repo(tmp_path), max_page_size=2, max_scan=4)
    with pytest.raises(QueryExportError):
        engine.query_events("s1", page_size=3)
    with pytest.raises(QueryExportError, match="cursor"):
        engine.query_events("s1", page_size=2, cursor="not-a-cursor")
