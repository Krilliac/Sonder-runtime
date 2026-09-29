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


def _tool_session(tmp_path, withheld: tuple[int, ...]):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    repo.append("s1", "user.message", {"content": "look it up", "turn_id": "t1"})
    repo.append("s1", "tool.call", {"call_id": "c1", "name": "search", "turn_id": "t1",
                                    "content": SECRET})
    repo.append("s1", "tool.result", {"call_id": "c1", "name": "search", "turn_id": "t1",
                                      "content": SECRET + "-result"})
    repo.append("s1", "tool.call", {"call_id": "c2", "name": "fetch", "turn_id": "t1",
                                    "content": "{}"})
    repo.append("s1", "tool.result", {"call_id": "c2", "name": "fetch", "turn_id": "t1",
                                      "content": "ok"})
    if withheld:
        repo.append("s1", "session.retention.applied", {"targets": [
            {"sequence": sequence, "privacy_class": "private"} for sequence in withheld]})
    return repo


@pytest.mark.parametrize("withheld", [(2, 3), (2,), (3,)], ids=["pair", "call-only", "result-only"])
def test_trajectory_survives_withheld_tool_events(tmp_path, withheld):
    result = HttpSessionFacade(_tool_session(tmp_path, withheld)).trajectory("s1")

    assert result.status_code == 200, result.body
    steps = result.body["steps"]
    assert SECRET not in str(result.body)
    # One action, one step: the withheld event never splits or strands it,
    # and the unaffected second action is still fully projected and paired.
    assert len(steps) == 2
    first, second = steps
    assert (second["call_id"], second["tool"], second["status"]) == ("c2", "fetch", "completed")
    assert first["status"] == "withheld"
    assert (first["requested_sequence"], first["completed_sequence"]) == (2, 3)
    assert (first["result_sha256"], first["result_bytes"]) == (None, None)
    if 2 in withheld:
        # The action itself was withheld: nothing identifying it survives.
        assert "search" not in str(result.body)
        assert (first["call_id"], first["tool"], first["arguments_sha256"]) == ("", "", "")
    else:
        # Only the observation was withheld; the visible call keeps its metadata.
        assert (first["call_id"], first["tool"]) == ("c1", "search")


def test_generic_redaction_is_not_mistaken_for_retention(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    repo.append("s1", "user.message",
                {"content": "token=super-secret", "redacted": True, "turn_id": "t1"})

    [item] = SessionQueryEngine(repo).export_transcript("s1")

    assert item.redacted is False
    assert item.content == "token=[REDACTED]"
    assert item.turn_id == "t1"
    title = HttpSessionFacade(repo).list_sessions().body["sessions"][0]["title"]
    assert title == "token=[REDACTED]"
