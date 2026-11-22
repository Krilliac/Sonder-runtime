"""Every retention marker is honoured, however many a session holds.

``SessionQueryEngine._privacy_targets`` read only the first ``max_scan``
(1000) ``session.retention.applied`` markers, oldest first, with no
pagination and no overflow refusal.  A marker past that bound -- which can
withhold an EARLIER event -- was never read, so ``export_events`` returned
the withheld event's original payload.  Markers are now resolved completely
(a keyset scan once the bounded search fills), and a session too long to
scan is refused rather than exported.
"""
from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session import query_export
from sonder_runtime.application.session.query_export import (
    QueryExportError, SessionQueryEngine, retention_envelope,
)

SECRET = "PRIVATE-CONTENT"
MARKER = "session.retention.applied"


def _bulk_append(db_path, session_id, items):
    """Append a hash-chained run of events in one transaction.

    ``SQLiteSessionRepository.append`` opens a connection per event, which
    makes thousands of markers take minutes; this writes the identical rows
    (same canonical payload and chain hash) so integrity still verifies.
    """
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT sequence, event_hash FROM session_event WHERE session_id=? "
            "ORDER BY sequence DESC LIMIT 1", (session_id,),
        ).fetchone()
        sequence, previous = row if row else (0, None)
        for event_type, payload in items:
            sequence += 1
            event_id = "sev_bulk_%s_%d" % (session_id, sequence)
            occurred = "2026-01-01T00:00:00Z"
            payload_json = SQLiteSessionRepository._canonical_payload(payload)
            event_hash = SQLiteSessionRepository._hash(
                session_id, sequence, event_id, event_type, occurred, payload_json, previous,
            )
            conn.execute(
                "INSERT INTO session_event VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, sequence, event_id, event_type, occurred, payload_json,
                 previous, event_hash),
            )
            previous = event_hash


def _session_with_late_marker(tmp_path, marker_count, *, max_read_limit=10_000):
    """Seq 1 is private, seq 2 is filler; only the LAST marker withholds seq 1."""
    db_path = tmp_path / "sessions.db"
    repo = SQLiteSessionRepository(db_path, max_read_limit=max_read_limit)
    filler = {"targets": [{"sequence": 2, "privacy_class": "filler"}]}
    late = {"targets": [{"sequence": 1, "privacy_class": "private"}]}
    _bulk_append(db_path, "s", [
        ("user.message", {"content": SECRET}),
        ("model.response", {"content": "filler reply"}),
        *[(MARKER, filler)] * (marker_count - 1),
        (MARKER, late),
    ])
    return repo


@pytest.mark.parametrize("marker_count", [1001, 2500])
def test_export_honours_a_marker_past_the_scan_bound(tmp_path, marker_count):
    repo = _session_with_late_marker(tmp_path, marker_count)
    exported = SessionQueryEngine(repo).export_events("s", max_events=10)

    assert exported.events[0].sequence == 1
    assert exported.events[0].payload == retention_envelope("private")
    assert exported.events[0].redacted is True
    assert exported.transcript[0].content == "[redacted: private]"
    assert SECRET not in str(exported.to_dict())
    assert SECRET not in exported.to_jsonl()
    assert exported.integrity is not None and exported.integrity.valid


@pytest.mark.parametrize("marker_count", [1001, 2500])
def test_query_honours_a_marker_past_the_scan_bound(tmp_path, marker_count):
    repo = _session_with_late_marker(tmp_path, marker_count)
    page = SessionQueryEngine(repo).query_events("s", event_type="user.message", page_size=5)

    assert [record.sequence for record in page.records] == [1]
    assert page.records[0].payload == retention_envelope("private")
    assert SECRET not in str([record.to_dict() for record in page.records])


def test_a_stricter_adapter_read_ceiling_still_resolves_every_marker(tmp_path):
    repo = _session_with_late_marker(tmp_path, 130, max_read_limit=50)
    exported = SessionQueryEngine(repo, max_page_size=10, max_scan=100).export_events(
        "s", max_events=10,
    )
    assert exported.events[0].payload == retention_envelope("private")
    assert SECRET not in exported.to_jsonl()


def test_markers_below_the_bound_use_one_search_and_no_scan(tmp_path):
    repo = _session_with_late_marker(tmp_path, 5)
    calls = []
    original = repo.read_range

    def counting_read_range(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    repo.read_range = counting_read_range
    exported = SessionQueryEngine(repo).export_events("s", max_events=10, include_integrity=False)

    assert exported.events[0].payload == retention_envelope("private")
    assert len(calls) == 1  # the export read itself; markers needed no scan


def test_export_is_refused_when_markers_cannot_be_fully_resolved(tmp_path, monkeypatch):
    repo = _session_with_late_marker(tmp_path, 1001)
    monkeypatch.setattr(query_export, "_MAX_MARKER_SCAN_EVENTS", 500)
    engine = SessionQueryEngine(repo)

    with pytest.raises(QueryExportError, match="privacy markers"):
        engine.export_events("s", max_events=10)
    with pytest.raises(QueryExportError, match="privacy markers"):
        engine.query_events("s", page_size=5)


def test_a_session_of_exactly_the_scan_bound_is_resolved_and_one_more_is_refused(
    tmp_path, monkeypatch,
):
    """The bound is inclusive: a session of exactly ``_MAX_MARKER_SCAN_EVENTS``
    events is fully scanned and exported; one event more is refused."""
    # 130 markers + 2 events = 132; the 50-row adapter page fills, so the
    # engine takes the keyset scan (pages of 50, 50, 32).
    repo = _session_with_late_marker(tmp_path, 130, max_read_limit=50)
    engine = SessionQueryEngine(repo, max_page_size=10, max_scan=100)

    monkeypatch.setattr(query_export, "_MAX_MARKER_SCAN_EVENTS", 132)
    exported = engine.export_events("s", max_events=10)
    assert exported.events[0].payload == retention_envelope("private")
    assert SECRET not in exported.to_jsonl()

    monkeypatch.setattr(query_export, "_MAX_MARKER_SCAN_EVENTS", 131)
    with pytest.raises(QueryExportError, match="privacy markers"):
        engine.export_events("s", max_events=10)
