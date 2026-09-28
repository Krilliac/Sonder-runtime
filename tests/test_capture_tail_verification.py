"""Capture finalization verifies the new tail, not the whole history.

``_finalize_capture`` used to run ``crash_safe_replay`` over the entire
session, so once a session held more events than the replay bound (10,000 by
default) every further turn failed with "session history exceeds replay
bound" (HTTP 503).  It now proves only what the turn appended: the appended
events are exactly the stored tail, each hashes correctly, and the first one
links to its stored predecessor.  Full-chain proof remains the job of
``replay``/``crash_safe_replay``.
"""
from __future__ import annotations

import sqlite3

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.domain.common.errors import IntegrityFailure


def _turn(service, index, session="s"):
    return service.capture_turn(
        session, "turn-%d" % index, ModelRequest(prompt="p%d" % index, tier="code"),
        request_id="r%d" % index, user_message="u%d" % index,
        model_response="a%d" % index,
    )


def test_capture_keeps_working_past_the_replay_bound(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=100)
    service = SessionCaptureService(repository, replay_limit=7)
    turns = [_turn(service, index) for index in range(6)]  # 18 events > 7
    last = turns[-1]
    assert [event.sequence for event in last.appended] == [16, 17, 18]
    assert [record.sequence for record in last.export.events] == [16, 17, 18]
    assert last.export.integrity is not None and last.export.integrity.valid
    assert not last.export.truncated
    assert [item.content for item in last.export.transcript] == ["u5", "a5"]
    # Small sessions keep the full replay evidence.
    assert turns[1].replay is not None and turns[1].replay.crash_safe


def test_split_request_capture_keeps_working_past_the_replay_bound(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=100)
    service = SessionCaptureService(repository, replay_limit=5)
    for index in range(4):
        _turn(service, index)
    pending = service.begin_request(
        "s", "turn-x", ModelRequest(prompt="px", tier="code"), request_id="rx",
        user_message="ux",
    )
    completed = service.complete_request(pending, model_response="ax")
    assert [event.sequence for event in completed.appended] == [13, 14, 15]


class _TamperAfterAppend:
    """Real repository that corrupts stored rows right after chosen appends."""

    def __init__(self, repository, path, tamper):
        self._repository = repository
        self._path = path
        self._tamper = tamper
        self._max_read_limit = repository._max_read_limit
        self.armed = 0  # tamper right after the Nth next append

    def append(self, *args, **kwargs):
        event = self._repository.append(*args, **kwargs)
        if self.armed:
            self.armed -= 1
        if self.armed == 0 and getattr(self, "_fire", False):
            self._fire = False
            with sqlite3.connect(self._path) as conn:
                conn.execute("DROP TRIGGER IF EXISTS session_event_no_update")
                self._tamper(conn, event)
        return event

    def __getattr__(self, name):
        return getattr(self._repository, name)


def _tampering_service(tmp_path, tamper):
    path = tmp_path / "s.db"
    repository = SQLiteSessionRepository(path, max_read_limit=100)
    wrapper = _TamperAfterAppend(repository, path, tamper)
    return SessionCaptureService(wrapper, replay_limit=4), wrapper


def test_tail_capture_rejects_tampered_appended_event(tmp_path):
    def tamper(conn, event):
        conn.execute(
            "UPDATE session_event SET payload_json = ? WHERE session_id = ? AND sequence = ?",
            ('{"content":"forged","turn_id":"turn-3"}', event.session_id, event.sequence),
        )

    service, wrapper = _tampering_service(tmp_path, tamper)
    for index in range(3):
        _turn(service, index)
    # Tamper the user message (second append), not the request snapshot.
    wrapper.armed, wrapper._fire = 2, True
    with pytest.raises(IntegrityFailure, match="integrity"):
        _turn(service, 3)


def test_tail_capture_rejects_a_broken_predecessor_link(tmp_path):
    def tamper(conn, event):
        conn.execute(
            "UPDATE session_event SET event_hash = ? WHERE session_id = ? AND sequence = ?",
            ("0" * 64, event.session_id, event.sequence - 1),
        )

    service, wrapper = _tampering_service(tmp_path, tamper)
    for index in range(3):
        _turn(service, index)
    wrapper.armed, wrapper._fire = 1, True
    with pytest.raises(IntegrityFailure, match="integrity"):
        _turn(service, 3)


def test_ranged_integrity_links_to_the_stored_predecessor(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "s.db", max_read_limit=100)
    service = SessionCaptureService(repository, replay_limit=100)
    for index in range(3):
        _turn(service, index)
    report = repository.inspect_integrity("s", start_sequence=4, limit=100)
    assert report.valid, report.issues
    assert (report.first_sequence, report.last_sequence) == (4, 9)
