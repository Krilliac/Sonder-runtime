import json
import sqlite3

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository


def test_append_reads_in_order_and_is_append_only(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    first = repo.append("s1", "session.started", {"owner": "alice"}, occurred_at_utc="2026-01-01T00:00:00Z")
    second = repo.append("s1", "message.user", {"text": "hello"}, occurred_at_utc="2026-01-01T00:00:01Z")

    assert [event.sequence for event in repo.read_range("s1")] == [1, 2]
    assert second.previous_hash == first.event_hash
    with pytest.raises(sqlite3.IntegrityError):
        with repo._connect() as conn:
            conn.execute("UPDATE session_event SET event_type = 'changed' WHERE session_id = 's1'")
    with pytest.raises(sqlite3.IntegrityError):
        with repo._connect() as conn:
            conn.execute("DELETE FROM session_event WHERE session_id = 's1'")


def test_range_search_export_and_integrity_are_bounded(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=3)
    repo.append("s1", "message.user", {"text": "alpha"})
    repo.append("s1", "tool.result", {"text": "beta"})
    repo.append("s1", "message.user", {"text": "gamma"})

    assert [e.sequence for e in repo.read_range("s1", start_sequence=2, end_sequence=3, limit=2)] == [2, 3]
    assert [e.event_type for e in repo.search(session_id="s1", text="beta", limit=3)] == ["tool.result"]
    exported = repo.export("s1", start_sequence=1, end_sequence=2, limit=2)
    assert [json.loads(line)["sequence"] for line in exported.splitlines()] == [1, 2]
    assert repo.inspect_integrity("s1", limit=3).valid
    with pytest.raises(ValueError):
        repo.read_range("s1", limit=4)


def test_integrity_detects_tampering_without_mutating_store(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    repo.append("s1", "message.user", {"text": "safe"})
    with repo._connect() as conn:
        conn.execute("DROP TRIGGER session_event_no_update")
        conn.execute("UPDATE session_event SET payload_json = ? WHERE session_id = ?", ('{"text":"changed"}', "s1"))
    report = repo.inspect_integrity("s1")
    assert not report.valid
    assert {issue.code for issue in report.issues} == {"event_hash_mismatch"}
