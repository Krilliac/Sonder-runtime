"""An export's integrity report must cover the events it exports.

``export_events`` verified the chain from sequence one under the export's own
event bound, so an export starting past sequence one was vouched for by a
report over a different window: with a 3-event bound, ``start_sequence=4``
exported events 4-6 while integrity checked 1-3, and an altered event 5
exported with ``integrity.valid == True``.
"""
from __future__ import annotations

import sqlite3

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.query_export import SessionQueryEngine


def _session(tmp_path, *, max_read_limit=3, count=6):
    db_path = tmp_path / "sessions.db"
    repo = SQLiteSessionRepository(db_path, max_read_limit=max_read_limit)
    for index in range(count):
        repo.append("s", "user.message", {"content": "m%d" % index})
    return repo, db_path


def _tamper(db_path, sequence, payload_json):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TRIGGER session_event_no_update")
        conn.execute(
            "UPDATE session_event SET payload_json=? WHERE session_id='s' AND sequence=?",
            (payload_json, sequence),
        )
        conn.commit()
    finally:
        conn.close()


def test_altered_event_inside_a_ranged_export_fails_integrity(tmp_path):
    repo, db_path = _session(tmp_path)
    _tamper(db_path, 5, '{"content":"EVIL"}')
    engine = SessionQueryEngine(repo, max_page_size=3, max_scan=3)

    exported = engine.export_events("s", start_sequence=4, max_events=3)

    assert [event.sequence for event in exported.events] == [4, 5, 6]
    assert exported.integrity is not None
    assert exported.integrity.valid is False
    assert any(issue.sequence == 5 and issue.code == "event_hash_mismatch"
               for issue in exported.integrity.issues)


def test_ranged_export_report_names_the_exported_window(tmp_path):
    repo, _db_path = _session(tmp_path, max_read_limit=100, count=8)
    engine = SessionQueryEngine(repo, max_page_size=10, max_scan=100)

    exported = engine.export_events("s", start_sequence=4, max_events=3)

    assert [event.sequence for event in exported.events] == [4, 5, 6]
    report = exported.integrity
    assert report is not None and report.valid
    assert (report.first_sequence, report.last_sequence, report.checked_events) == (4, 6, 3)


def test_ranged_export_detects_a_broken_link_to_its_predecessor(tmp_path):
    repo, db_path = _session(tmp_path, max_read_limit=100, count=6)
    # Rewrite event 3's stored hash: event 4 (first exported) no longer links.
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TRIGGER session_event_no_update")
        conn.execute("UPDATE session_event SET event_hash=? WHERE session_id='s' AND sequence=3",
                     ("0" * 64,))
        conn.commit()
    finally:
        conn.close()
    engine = SessionQueryEngine(repo, max_page_size=10, max_scan=100)

    exported = engine.export_events("s", start_sequence=4, max_events=3)

    assert exported.integrity is not None and not exported.integrity.valid
    assert any(issue.sequence == 4 and issue.code == "previous_hash_mismatch"
               for issue in exported.integrity.issues)
