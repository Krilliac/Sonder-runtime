"""Adversarial canaries for bounded complete session recovery (issue #510, PR #542).

Covers page boundaries, truncated and cycled cursors, concurrent appends
during paging, cross-session isolation, and recovery after restart.
"""
from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository


def _seed(repo, count, *, session_id="s1", prefix="e"):
    for index in range(count):
        repo.append(session_id, "model.response", {"content": f"{prefix}{index}"},
                    event_id=f"{session_id}-{prefix}{index}")


def _tamper(database, statement, params=()):
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER IF EXISTS session_event_no_update")
        connection.execute("DROP TRIGGER IF EXISTS session_event_no_delete")
        connection.execute(statement, params)


# -- page boundaries -------------------------------------------------------

@pytest.mark.parametrize("count", [0, 1, 2, 3, 4, 5, 6])
def test_page_boundary_counts_recover_exactly(tmp_path, count):
    repo = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=2)
    _seed(repo, count)

    recovered = repo.read_complete("s1", max_events=6)

    assert [event.sequence for event in recovered] == list(range(1, count + 1))


def test_bound_equal_to_full_page_multiple_is_accepted(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=2)
    _seed(repo, 4)

    assert len(repo.read_complete("s1", max_events=4)) == 4


def test_one_event_past_full_page_bound_fails_closed(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=2)
    _seed(repo, 5)

    with pytest.raises(ValueError, match="exceeds recovery bound"):
        repo.read_complete("s1", max_events=4)


@pytest.mark.parametrize("bad", [0, -1, 100_001, True, 2.0, "10"])
def test_invalid_bound_is_rejected(tmp_path, bad):
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    with pytest.raises(ValueError, match="max_events"):
        repo.read_complete("s1", max_events=bad)


# -- truncated cursors -----------------------------------------------------

def test_head_truncation_fails_closed(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 5)
    _tamper(database, "DELETE FROM session_event WHERE session_id='s1' AND sequence=1")

    with pytest.raises(ValueError, match="contiguous"):
        repo.read_complete("s1", max_events=8)


@pytest.mark.parametrize("missing", [2, 3, 4])
def test_gap_at_or_across_page_boundary_fails_closed(tmp_path, missing):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 6)
    _tamper(database, "DELETE FROM session_event WHERE session_id='s1' AND sequence=?", (missing,))

    with pytest.raises(ValueError, match="contiguous"):
        repo.read_complete("s1", max_events=8)


def test_displaced_sequence_is_reported_as_corruption_not_overflow(tmp_path):
    """A moved row must not make the cursor re-read (and double-count) rows.

    With an offset-derived cursor, sequences 1,2,4,5,9 paged by two re-read
    sequence 5, so a within-bound corrupt history was misreported as a
    recovery-bound overflow instead of an integrity failure.
    """
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 5)
    _tamper(database, "UPDATE session_event SET sequence=9 WHERE session_id='s1' AND sequence=3")

    with pytest.raises(ValueError, match="contiguous"):
        repo.read_complete("s1", max_events=5)


def test_tail_truncation_is_not_detectable_by_the_chain_alone(tmp_path):
    """Documented limitation: without an external head anchor, dropping the
    newest events leaves a valid shorter chain. Pin the behaviour so a future
    head-anchor change has to update this test deliberately."""
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 5)
    _tamper(database, "DELETE FROM session_event WHERE session_id='s1' AND sequence>=4")

    assert [event.sequence for event in repo.read_complete("s1", max_events=8)] == [1, 2, 3]


# -- cycled / forged cursors ----------------------------------------------

def test_previous_hash_cycle_fails_closed(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 4)
    first_hash = repo.read_range("s1", limit=1)[0].event_hash
    _tamper(database, "UPDATE session_event SET previous_hash=? WHERE session_id='s1' AND sequence=4",
            (first_hash,))

    with pytest.raises(ValueError, match="contiguous"):
        repo.read_complete("s1", max_events=8)


def test_swapped_payloads_across_pages_fail_integrity(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 4)
    _tamper(database, "UPDATE session_event SET payload_json=("
                      "SELECT payload_json FROM session_event WHERE session_id='s1' AND sequence=4"
                      ") WHERE session_id='s1' AND sequence=1")

    with pytest.raises(ValueError, match="integrity"):
        repo.read_complete("s1", max_events=8)


def test_event_grafted_from_another_session_fails_integrity(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 3, session_id="s1")
    _seed(repo, 3, session_id="s2")
    _tamper(database, "DELETE FROM session_event WHERE session_id='s1' AND sequence=3")
    _tamper(database, "UPDATE session_event SET session_id='s1' WHERE session_id='s2' AND sequence=3")

    with pytest.raises(ValueError, match="(contiguous|integrity)"):
        repo.read_complete("s1", max_events=8)


def test_interleaved_sessions_recover_independently(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=2)
    for index in range(5):
        repo.append("a", "model.response", {"content": f"a{index}"}, event_id=f"a{index}")
        repo.append("b", "model.response", {"content": f"b{index}"}, event_id=f"b{index}")

    assert [e.event_id for e in repo.read_complete("a", max_events=5)] == [f"a{i}" for i in range(5)]
    assert [e.event_id for e in repo.read_complete("b", max_events=5)] == [f"b{i}" for i in range(5)]


# -- concurrent appends during paging --------------------------------------

def test_append_between_pages_is_excluded_from_snapshot_then_visible(tmp_path, monkeypatch):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 5)
    writer_repo = SQLiteSessionRepository(database, max_read_limit=2)
    outcome: dict[str, object] = {}
    started = threading.Event()

    def append_late():
        started.set()
        try:
            outcome["event"] = writer_repo.append(
                "s1", "model.response", {"content": "late"}, event_id="late")
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            outcome["error"] = exc

    writer = threading.Thread(target=append_late)
    original = SQLiteSessionRepository._row_to_event
    calls = {"n": 0}

    def row_hook(row):
        calls["n"] += 1
        if calls["n"] == 2:  # end of the first page, before page two is read
            writer.start()
            started.wait(5)
            time.sleep(0.3)
            outcome["writer_blocked_mid_read"] = writer.is_alive()
        return original(row)

    monkeypatch.setattr(SQLiteSessionRepository, "_row_to_event", staticmethod(row_hook))
    recovered = repo.read_complete("s1", max_events=8)
    monkeypatch.setattr(SQLiteSessionRepository, "_row_to_event", staticmethod(original))
    writer.join(timeout=10)

    assert not writer.is_alive()
    assert "error" not in outcome, outcome.get("error")
    assert outcome["writer_blocked_mid_read"] is True
    assert [event.sequence for event in recovered] == [1, 2, 3, 4, 5]
    after = repo.read_complete("s1", max_events=8)
    assert [event.event_id for event in after][-1] == "late"
    assert [event.sequence for event in after] == [1, 2, 3, 4, 5, 6]


def test_concurrent_appenders_never_yield_a_torn_history(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 3)
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer():
        index = 0
        while not stop.is_set() and index < 60:
            try:
                repo.append("s1", "model.response", {"content": f"w{index}"}, event_id=f"w{index}")
            except BaseException as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)
                return
            index += 1

    thread = threading.Thread(target=writer)
    thread.start()
    lengths = []
    try:
        for _ in range(20):
            recovered = repo.read_complete("s1", max_events=100)
            assert [event.sequence for event in recovered] == list(range(1, len(recovered) + 1))
            lengths.append(len(recovered))
    finally:
        stop.set()
        thread.join(timeout=30)
    assert not errors, errors
    assert lengths == sorted(lengths)


# -- restart ---------------------------------------------------------------

def test_recovery_after_restart_matches_pre_restart_history(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database, max_read_limit=2)
    _seed(repo, 7)
    before = repo.read_complete("s1", max_events=10)
    assert repo.close(timeout=5) is True

    reopened = SQLiteSessionRepository(database, max_read_limit=3)
    after = reopened.read_complete("s1", max_events=10)

    assert after == before
    reopened.append("s1", "model.response", {"content": "post-restart"}, event_id="post")
    resumed = reopened.read_complete("s1", max_events=10)
    assert resumed[-1].previous_hash == before[-1].event_hash
    assert resumed[-1].sequence == 8


def test_closed_repository_refuses_recovery(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    _seed(repo, 1)
    assert repo.close(timeout=5) is True

    with pytest.raises(RuntimeError, match="closed"):
        repo.read_complete("s1")


# -- legacy oversized events (PR #542 review P3-b) --------------------------

def _insert_legacy(repo, database, payload, *, session_id="s1", tamper=False):
    payload_json = __import__("json").dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    occurred_at = "2026-01-01T00:00:00Z"
    event_hash = repo._hash(session_id, 1, "legacy", "tool.result", occurred_at, payload_json, None)
    stored = payload_json.replace("x", "y", 1) if tamper else payload_json
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO session_event VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, 1, "legacy", "tool.result", occurred_at, stored, None, event_hash),
        )


def test_legacy_event_over_append_cap_remains_recoverable_and_reportable(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database)
    _insert_legacy(repo, database, {"content": "x" * (9 * 1024 * 1024)})

    recovered = repo.read_complete("s1", max_events=4)
    report = repo.inspect_integrity("s1")

    assert [event.event_id for event in recovered] == ["legacy"]
    assert report.valid is True and report.checked_events == 1
    appended = repo.append("s1", "model.response", {"content": "after legacy"})
    assert appended.previous_hash == recovered[0].event_hash
    with pytest.raises(ValueError, match="payload exceeds"):
        repo.append("s1", "tool.result", {"content": "x" * (9 * 1024 * 1024)})


def test_tampered_legacy_oversized_event_is_reported_not_raised(tmp_path):
    database = tmp_path / "s.db"
    repo = SQLiteSessionRepository(database)
    _insert_legacy(repo, database, {"content": "x" * (9 * 1024 * 1024)}, tamper=True)

    report = repo.inspect_integrity("s1")

    assert report.valid is False
    assert [issue.code for issue in report.issues] == ["event_hash_mismatch"]
    with pytest.raises(ValueError, match="integrity"):
        repo.read_complete("s1", max_events=4)
