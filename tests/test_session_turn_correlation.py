"""Interaction rows vs the session event log: no duplicate, no resurrection,
pre-log rows kept in order, fail closed (event_turns + session_atif_export).
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import memory_store
from sonder_runtime.adapters import session_atif_export
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session import event_turns
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session.query_export import QueryExportError, SessionQueryEngine
from sonder_runtime.domain.common.errors import IntegrityFailure

SID = "s"
CRED_PROMPT = "PRIVATE-CANARY-94 Authorization: Bearer abcdefghijklmnop1234"


@pytest.fixture
def store(tmp_path):
    mem_path = tmp_path / "mem.db"
    sessions_path = tmp_path / "sessions.db"
    repo = SQLiteSessionRepository(sessions_path)
    conn = memory_store.connect(mem_path)
    try:
        memory_store.touch_session(conn, SID, "")
    finally:
        conn.close()
    return mem_path, sessions_path, repo


def row(store, iid, task, response, ts=None):
    mem_path, _sessions_path, _repo = store
    conn = memory_store.connect(mem_path)
    try:
        memory_store.log_interaction(conn, iid, task, "", response, "code", session_id=SID)
        if ts is not None:
            conn.execute("UPDATE interactions SET ts=? WHERE id=?", (ts, iid))
            conn.commit()
    finally:
        conn.close()


def shift(store, iid, seconds):
    """Move a row ``seconds`` earlier (SQLite UTC clock)."""
    mem_path, _sessions_path, _repo = store
    conn = memory_store.connect(mem_path)
    try:
        conn.execute("UPDATE interactions SET ts=datetime(ts, ?) WHERE id=?",
                     ("-%d seconds" % seconds, iid))
        conn.commit()
    finally:
        conn.close()


def retro(repo, n, user, reply, request_id=None):
    """A retrospective capture: appended after the turn, no provider evidence."""
    SessionCaptureService(repo).capture_turn(
        SID, "t%d" % n, ModelRequest(prompt=user, tier="fast"),
        request_id=request_id or "r%d" % n, user_message=user, model_response=reply)


def admitted(repo, n, user, reply, write_row=None):
    """An admitted turn: request + provider evidence first, the row, then the reply."""
    capture = SessionCaptureService(repo)
    pending = capture.begin_request(SID, "t%d" % n, ModelRequest(prompt=user, tier="fast"),
                                    request_id="r%d" % n, user_message=user)
    attempt = capture.begin_provider_attempt(pending, provider="ollama",
                                             operation="/api/chat", payload={"model": "m"})
    capture.finish_provider_attempt(pending, attempt, response={"done": True})
    if write_row is not None:
        write_row()
    capture.complete_request(pending, model_response=reply)


def withhold(repo, event_type, needle, label="private"):
    event = next(e for e in repo.read_complete(SID)
                 if e.event_type == event_type and needle in str(e.payload.get("content")))
    repo.append(SID, "session.retention.applied", {
        "privacy_class": "public_metadata", "action": "redact_or_expire",
        "targets": [{"event_id": event.event_id, "sequence": event.sequence, "privacy_class": label}]})


def export_turns(store, repo=None, limit=200):
    mem_path, _sessions_path, default_repo = store
    target_repo = default_repo if repo is None else repo
    conn = memory_store.connect(mem_path)
    try:
        rows = memory_store.session_turn_rows(conn, SID)
        return event_turns.fuller_turns(lambda: target_repo, SID, rows)[-limit:]
    finally:
        conn.close()


def export_atif(store, repo=None, limit=200):
    mem_path, _sessions_path, default_repo = store
    target_repo = default_repo if repo is None else repo
    conn = memory_store.connect(mem_path)
    try:
        sess = memory_store.get_session(conn, SID)
        return session_atif_export.interaction_session_atif(
            conn, SID, sess, limit=limit, get_repository=lambda: target_repo,
        )
    finally:
        conn.close()


def listing_count(store, repo=None):
    mem_path, _sessions_path, default_repo = store
    target_repo = default_repo if repo is None else repo
    conn = memory_store.connect(mem_path)
    try:
        counts = event_turns.merged_turn_counts(
            lambda: target_repo,
            lambda sid: memory_store.session_turn_rows(conn, sid),
            [SID],
        )
        return counts.get(SID)
    finally:
        conn.close()


def users(turns):
    return [t["task"] for t in turns]


def atif_users(raw):
    return [s["message"] for s in json.loads(raw)["steps"] if s["source"] == "user"]


# ---- duplication ----

def test_learning_turn_whose_prompt_capture_redacted_is_listed_once(store):
    _m, _s, repo = store
    row(store, "I1", CRED_PROMPT, "ans")
    retro(repo, 1, CRED_PROMPT, "ans")
    turns = export_turns(store)
    assert len(users(turns)) == 1
    assert len(atif_users(export_atif(store))) == 1
    assert listing_count(store) == 1


def test_unlinked_retrospective_row_seconds_before_its_capture_is_still_dropped(store):
    _m, _s, repo = store
    row(store, "I1", CRED_PROMPT, "ans")
    shift(store, "I1", 30)
    retro(repo, 1, CRED_PROMPT, "ans")
    assert len(users(export_turns(store))) == 1


def test_row_named_by_a_logged_request_id_is_dropped_however_early(store):
    _m, _s, repo = store
    row(store, "I1", CRED_PROMPT, "ans")
    shift(store, "I1", 3 * 3600)
    retro(repo, 1, CRED_PROMPT, "ans", request_id="I1")
    assert len(users(export_turns(store))) == 1


def test_rows_inside_the_logged_span_are_never_emitted(store):
    _m, _s, repo = store
    admitted(repo, 1, CRED_PROMPT, "ans",
             write_row=lambda: row(store, "I1", CRED_PROMPT, "ans"))
    row(store, "W1", "weather in Paris", "routed reply")
    turns = export_turns(store)
    assert len(turns) == 1 and "PRIVATE-CANARY-94" in str(turns[0]["task"])
    assert all("routed reply" not in str(t["response"]) for t in turns)


# ---- resurrection ----

def test_withheld_learning_turn_never_resurrects_through_its_row(store):
    _m, _s, repo = store
    row(store, "I1", CRED_PROMPT, "PRIVATE-REPLY-95")
    retro(repo, 1, CRED_PROMPT, "PRIVATE-REPLY-95")
    withhold(repo, "user.message", "PRIVATE-CANARY-94")
    withhold(repo, "model.response", "PRIVATE-REPLY-95")
    turns = export_turns(store)
    raw = export_atif(store)
    for secret in ("PRIVATE-CANARY-94", "PRIVATE-REPLY-95"):
        assert secret not in str(turns) and secret not in raw
    assert users(turns) == ["[redacted: private]"]
    assert listing_count(store) == 1


def test_withheld_reply_alone_is_withheld(store):
    _m, _s, repo = store
    retro(repo, 1, "hello", "PRIVATE-REPLY-93")
    withhold(repo, "model.response", "PRIVATE-REPLY-93")
    turns = export_turns(store)
    assert "PRIVATE-REPLY-93" not in str(turns)
    assert turns[0]["response"] == "[redacted: private]"
    assert "PRIVATE-REPLY-93" not in export_atif(store)


def test_withheld_turn_exports_no_turn_id(store):
    _m, _s, repo = store
    retro(repo, 1, "PRIVATE-CANARY-90", "r")
    withhold(repo, "user.message", "PRIVATE-CANARY-90")
    steps = json.loads(export_atif(store))["steps"]
    user = next(s for s in steps if s["source"] == "user")
    assert "interaction_id" not in (user.get("extra") or {})


# ---- pre-log rows are kept, in order ----

def test_pre_log_row_survives_a_later_log_turn_with_the_same_prompt(store):
    _m, _s, repo = store
    row(store, "I0", "continue", "OLD-ANSWER-1", ts="2020-01-01 00:00:00")
    retro(repo, 1, "continue", "NEW-ANSWER-2")
    turns = export_turns(store)
    assert users(turns) == ["continue", "continue"]
    assert [t["response"] for t in turns] == ["OLD-ANSWER-1", "NEW-ANSWER-2"]
    assert listing_count(store) == 2


def test_admitted_first_turn_keeps_rows_from_an_earlier_second(store):
    _m, _s, repo = store
    row(store, "I0", "older question", "older answer")
    shift(store, "I0", 5)
    admitted(repo, 1, "new question", "new answer")
    assert users(export_turns(store)) == ["older question", "new question"]


def test_no_repository_keeps_the_rows(store):
    row(store, "I0", "only row", "a")
    mem_path, _s, _r = store
    conn = memory_store.connect(mem_path)
    try:
        rows = memory_store.session_turn_rows(conn, SID)
        app = SimpleNamespace(config=None)  # no session_repository attribute
        assert event_turns.repository_of(app) is None
        turns = event_turns.fuller_turns(lambda: event_turns.repository_of(app), SID, rows)
        assert users(turns) == ["only row"]
    finally:
        conn.close()


# ---- fail closed ----

@pytest.mark.parametrize("marker", [
    {"targets": "nope"},
    {"targets": [{"sequence": "x", "privacy_class": "p"}]},
    {"targets": [{"sequence": 0, "privacy_class": "p"}]},
    {"targets": [{"sequence": 2, "privacy_class": None}]},
    {"targets": ["not-a-mapping"]},
])
def test_malformed_marker_fails_closed(store, marker):
    _m, _s, repo = store
    row(store, "I1", "PRIVATE-CANARY-96", "PRIVATE-REPLY-97", ts="2020-01-01 00:00:00")
    retro(repo, 1, "PRIVATE-CANARY-98", "PRIVATE-REPLY-99")
    repo.append(SID, "session.retention.applied", marker)
    with pytest.raises(IntegrityFailure):
        export_turns(store)
    with pytest.raises(IntegrityFailure):
        export_atif(store)
    assert listing_count(store) is None
    with pytest.raises(QueryExportError):
        SessionQueryEngine(repo).export_events(SID)


def test_tampered_log_fails_closed(store):
    _m, sessions_path, repo = store
    row(store, "I1", "PRIVATE-CANARY-96", "PRIVATE-REPLY-97")
    retro(repo, 1, "PRIVATE-CANARY-96", "PRIVATE-REPLY-97")
    db = sqlite3.connect(sessions_path)
    try:
        db.execute("DROP TRIGGER session_event_no_update")
        db.execute("UPDATE session_event SET payload_json=? WHERE sequence=2",
                   (json.dumps({"content": "tampered", "turn_id": "t1"}),))
        db.commit()
    finally:
        db.close()
    with pytest.raises(IntegrityFailure):
        export_turns(store)
    assert listing_count(store) is None


def test_read_bound_fails_closed(monkeypatch, store):
    _m, _s, repo = store
    row(store, "I1", "PRIVATE-CANARY-96", "PRIVATE-REPLY-97")

    def over_bound(*_args, **_kwargs):
        raise ValueError("session history exceeds recovery bound")

    monkeypatch.setattr(repo, "read_complete", over_bound)
    with pytest.raises(IntegrityFailure):
        export_turns(store)


# ---- listing == export ----

@pytest.mark.parametrize("scenario", ["redacted", "withheld", "mixed"])
def test_listing_count_matches_the_export(store, scenario):
    _m, _s, repo = store
    if scenario == "redacted":
        row(store, "I1", CRED_PROMPT, "ans")
        retro(repo, 1, CRED_PROMPT, "ans")
    elif scenario == "withheld":
        row(store, "I1", CRED_PROMPT, "PRIVATE-REPLY-95")
        retro(repo, 1, CRED_PROMPT, "PRIVATE-REPLY-95")
        withhold(repo, "user.message", "PRIVATE-CANARY-94")
    else:
        row(store, "O1", "old", "a", ts="2020-01-01 00:00:00")
        row(store, "O2", "old", "b", ts="2020-01-01 00:00:01")
        retro(repo, 1, "old", "c")
        retro(repo, 2, CRED_PROMPT, "d")
    assert listing_count(store) == len(export_turns(store))


# ---- unit: turns_from_events & anchor-second boundary ----

def _ev(seq, kind, ts, **payload):
    return SimpleNamespace(sequence=seq, event_type=kind, occurred_at_utc=ts,
                           payload=payload, event_id="e%d" % seq)


def test_event_turns_group_by_turn_and_tolerate_unknown_events():
    def ev(kind, **payload):
        return SimpleNamespace(event_type=kind, payload=payload, event_id="e", occurred_at_utc="t")

    turns = event_turns.turns_from_events([
        ev("model.requested", turn_id="a", tier="fast"),
        ev("user.message", turn_id="a", content="hi"),
        ev("provider.requested", turn_id="a"),
        ev("model.response", turn_id="a", content="yo"),
        ev("model.response", turn_id="orphan", content="ignored"),
        ev("user.message", turn_id="b", content="fail"),
        ev("model.failed", turn_id="b", error_code="DEPENDENCY_UNAVAILABLE"),
    ])
    assert [(t["task"], t["response"], t["tier"]) for t in turns] == [
        ("hi", "yo", "fast"),
        ("fail", "[no response: DEPENDENCY_UNAVAILABLE]", ""),
    ]


def test_pre_log_boundary_is_the_anchor_second():
    events = [_ev(1, "model.requested", "2026-01-01T10:00:00.900000Z", turn_id="a", request_id="r"),
              _ev(2, "user.message", "2026-01-01T10:00:00.950000Z", turn_id="a", content="q"),
              _ev(3, "provider.requested", "2026-01-01T10:00:01Z", turn_id="a", request_id="r"),
              _ev(4, "model.response", "2026-01-01T10:00:09Z", turn_id="a", content="x")]
    rows = [{"id": "keep", "ts": "2026-01-01 09:59:59"},
            {"id": "same-second", "ts": "2026-01-01 10:00:00"},
            {"id": "later", "ts": "2026-01-01 10:00:05"},
            {"id": "no-ts", "ts": None}]
    assert [r["id"] for r in event_turns._pre_log_rows(events, rows)] == ["keep"]
    unadmitted = [e for e in events if e.event_type != "provider.requested"]
    assert event_turns._pre_log_rows(unadmitted, rows) == []
    old = [{"id": "old", "ts": "2026-01-01 08:59:59"}]
    assert [r["id"] for r in event_turns._pre_log_rows(unadmitted, old)] == ["old"]
    lone = [_ev(1, "model.requested", "2026-01-01T10:00:00Z", turn_id="z")]
    assert event_turns._pre_log_rows(lone, rows) == rows


def test_empty_turn_id_model_requested_does_not_steal_anchor():
    events = [
        _ev(1, "model.requested", "2026-01-01T08:00:00Z", turn_id=""),
        _ev(2, "model.requested", "2026-01-01T10:00:00Z", turn_id="t1"),
        _ev(3, "user.message", "2026-01-01T10:00:00Z", turn_id="", content="q"),
        _ev(4, "provider.requested", "2026-01-01T10:00:01Z", turn_id="t1"),
    ]
    rows = [{"id": "keep", "ts": "2026-01-01 09:00:00"}]
    # Anchor should be seq 3 (10:00:00), not seq 1 (08:00:00) with empty turn_id.
    assert [r["id"] for r in event_turns._pre_log_rows(events, rows)] == []  # unadmitted empty turn_id -> guard
    events[2] = _ev(3, "user.message", "2026-01-01T10:00:00Z", turn_id="t1", content="q")
    assert [r["id"] for r in event_turns._pre_log_rows(events, rows)] == ["keep"]
