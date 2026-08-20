from __future__ import annotations

import pytest

from sonder_runtime.application.session.replay import (
    reconstruct_model_request,
    reconstruct_transcript,
    replay_session,
)
from sonder_runtime.domain.common.errors import IntegrityFailure
from sonder_runtime.domain.common.events import DomainEvent


def event(sequence, event_type, payload=None, *, session="s1"):
    return DomainEvent(event_type, "session", session, sequence, payload or {})


def stream():
    return [
        event(1, "session.started", {"turn_id": "t1"}),
        event(2, "user.message", {"turn_id": "t1", "content": "hello"}),
        event(3, "model.requested", {
            "turn_id": "t1", "prompt": "hello", "tier": "local",
            "system": "be brief", "history": (("user", "hello"),),
            "options": {"temperature": 0}, "stream": False,
        }),
        event(4, "tool.call", {"turn_id": "t1", "content": "search", "name": "web"}),
        event(5, "tool.result", {"turn_id": "t1", "content": "answer", "name": "web"}),
        event(6, "model.response", {"turn_id": "t1", "content": "done"}),
        event(7, "session.closed"),
    ]


def test_replay_reconstructs_all_outputs_from_events():
    replay = replay_session(stream())
    assert replay.request.prompt == "hello"
    assert replay.request.options["temperature"] == 0
    assert [message.role for message in replay.transcript] == ["user", "tool", "tool", "assistant"]
    assert replay.projection.status == "closed"
    assert replay.projection.turn_count == 1
    assert replay.projection.tool_call_count == 1
    assert replay.projection.tool_result_count == 1


def test_replay_is_order_independent_but_deterministic():
    first = replay_session(stream())
    second = replay_session(list(reversed(stream())))
    assert first == second


def test_request_options_cannot_be_mutated_through_replay():
    request = reconstruct_model_request(stream())
    with pytest.raises(TypeError):
        request.options["new"] = 1


def test_gap_duplicate_and_cross_session_streams_fail_closed():
    for bad in (
        [event(1, "session.started"), event(3, "session.closed")],
        [event(1, "session.started"), event(1, "session.closed")],
        [event(1, "session.started"), event(2, "session.closed", session="other")],
    ):
        with pytest.raises(IntegrityFailure):
            replay_session(bad)


def test_missing_model_snapshot_does_not_infer_request_from_transcript():
    assert reconstruct_model_request([
        event(1, "user.message", {"content": "not a request"}),
    ]) is None
