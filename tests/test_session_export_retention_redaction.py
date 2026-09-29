"""Retention-redacted transcript events must export without crashing or leaking.

A ``session.retention.applied`` marker replaces the targeted event's payload
with ``{"privacy_class": ..., "redacted": True}``.  Transcript projection used
to index ``payload["content"]`` on such records and raise ``KeyError``, which
broke every ``export_events`` caller (HTTP export/replay/trajectory, session
titles, capture finalization).
"""

from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.application.session.query_export import SessionQueryEngine

SECRET = "SECRET-CANARY-7f3a"

_TRANSCRIPT_EVENTS = (
    ("user.message", "user"),
    ("model.response", "assistant"),
    ("tool.call", "tool"),
    ("tool.result", "tool"),
    ("message.received", "user"),
    ("message.emitted", "assistant"),
)


def _redacted_session(tmp_path, event_type: str = "user.message"):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    repo.append("s1", event_type, {"content": SECRET, "turn_id": "t1"})
    repo.append("s1", "model.response", {"content": "visible reply", "turn_id": "t1"})
    repo.append("s1", "session.retention.applied",
                {"targets": [{"sequence": 1, "privacy_class": "private"}]})
    return repo


@pytest.mark.parametrize("event_type, role", _TRANSCRIPT_EVENTS)
def test_export_events_keeps_a_redacted_placeholder_turn(tmp_path, event_type, role):
    exported = SessionQueryEngine(_redacted_session(tmp_path, event_type)).export_events("s1")

    assert [(item.role, item.sequence) for item in exported.transcript] == [
        (role, 1), ("assistant", 2),
    ]
    placeholder = exported.transcript[0]
    assert placeholder.content == "[redacted: private]"
    assert placeholder.redacted is True
    assert placeholder.event_type == event_type
    assert exported.transcript[1].content == "visible reply"
    assert exported.transcript[1].redacted is False
    assert SECRET not in str(exported.to_dict())


def test_transcript_skips_unredacted_event_without_string_content(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    repo.append("s1", "tool.call", {"name": "search", "arguments": {"q": "x"}})
    repo.append("s1", "user.message", {"content": "hi"})

    transcript = SessionQueryEngine(repo).export_transcript("s1")

    assert [(item.role, item.content) for item in transcript] == [("user", "hi")]


def test_http_export_replay_and_trajectory_survive_retention(tmp_path):
    facade = HttpSessionFacade(_redacted_session(tmp_path), max_replay_events=10)

    export = facade.export("s1")
    assert export.status_code == 200
    assert export.body["transcript"][0]["content"] == "[redacted: private]"
    assert SECRET not in str(export.body)

    replay = facade.replay("s1")
    assert replay.status_code == 200
    assert [item["sequence"] for item in replay.body["transcript"]] == [1, 2]
    assert SECRET not in str(replay.body)

    trajectory = facade.trajectory("s1")
    assert trajectory.status_code == 200
    assert SECRET not in str(trajectory.body)


def test_list_sessions_title_never_shows_redacted_turn(tmp_path):
    repo = _redacted_session(tmp_path)
    repo.append("s1", "user.message", {"content": "second question", "turn_id": "t2"})
    repo.append("s2", "user.message", {"content": SECRET})
    repo.append("s2", "session.retention.applied",
                {"targets": [{"sequence": 1, "privacy_class": "private"}]})

    result = HttpSessionFacade(repo).list_sessions()

    assert result.status_code == 200
    titles = {item["id"]: item["title"] for item in result.body["sessions"]}
    assert titles == {"s1": "second question", "s2": ""}
    assert SECRET not in str(result.body)
    assert "redacted" not in str(titles)
