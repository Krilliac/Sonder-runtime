from __future__ import annotations

import pytest

from sonder_runtime.application.session.fork import ForkBoundary, fork_session
from sonder_runtime.domain.common.errors import IntegrityFailure, InvalidInput
from sonder_runtime.domain.common.events import DomainEvent
from sonder_runtime.domain.common.ids import SessionId


def event(sequence: int, event_type: str, session: SessionId, payload=None) -> DomainEvent:
    return DomainEvent(event_type, "session", session.serialize(), sequence, payload or {})


def stream(session: SessionId) -> list[DomainEvent]:
    return [event(1, "session.started", session), event(2, "user.message", session, {"content": "hi"}), event(3, "session.paused", session)]


def test_fork_inherits_inclusive_prefix_and_records_lineage():
    parent = SessionId.new()
    child = SessionId.new()
    source = stream(parent)

    result = fork_session(source, ForkBoundary(2), child_session_id=child)

    assert result.child_session_id == child
    assert result.lineage.parent_session_id == parent
    assert result.lineage.boundary_sequence == 2
    assert result.lineage.boundary_event_id == source[1].id
    assert result.next_sequence == 3
    assert result.inherited_events == tuple(source[:2])
    assert all(item.aggregate_id == parent.serialize() for item in result.inherited_events)


def test_boundary_event_id_must_match_sequence():
    parent = SessionId.new()
    with pytest.raises(IntegrityFailure):
        fork_session(stream(parent), ForkBoundary(2, event_id=stream(parent)[0].id))


@pytest.mark.parametrize("boundary", [0, -1, True, 4])
def test_invalid_boundaries_fail_closed(boundary):
    parent = SessionId.new()
    with pytest.raises(InvalidInput):
        fork_session(stream(parent), boundary)


def test_source_stream_must_be_typed_contiguous_single_session():
    parent = SessionId.new()
    bad = [event(1, "session.started", parent), event(3, "session.paused", parent)]
    with pytest.raises(IntegrityFailure):
        fork_session(bad, 1)
    with pytest.raises(InvalidInput):
        fork_session([DomainEvent("session.started", "session", "s1", 1)], 1)


def test_fork_does_not_mutate_or_relabel_source_events():
    parent = SessionId.new()
    source = stream(parent)
    result = fork_session(source, 1)
    assert source[0].aggregate_id == parent.serialize()
    assert result.inherited_events[0] is source[0]
