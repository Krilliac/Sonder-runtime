from __future__ import annotations

import pytest

from sonder_runtime.application.session.checkpoints import (
    ProjectionCheckpoint,
    checkpoint_projection,
    create_projection_checkpoint,
)
from sonder_runtime.application.session.projections import project_session
from sonder_runtime.domain.common.errors import IntegrityFailure, InvalidInput
from sonder_runtime.domain.common.events import DomainEvent


def event(sequence: int, event_type: str, payload=None) -> DomainEvent:
    return DomainEvent(event_type, "session", "s1", sequence, payload or {})


def stream():
    return [
        event(1, "session.started"),
        event(2, "user.message", {"content": "hello"}),
    ]


def test_checkpoint_captures_version_and_exact_source_identity():
    projection = project_session(stream())
    checkpoint = create_projection_checkpoint(
        projection, source_sequence=2, source_hash="hash-at-2", projection_version=3
    )

    assert checkpoint.projection == projection
    assert checkpoint.projection_version == 3
    assert checkpoint.source_sequence == 2
    assert checkpoint.source_hash == "hash-at-2"
    assert checkpoint.session_id == "s1"
    assert not checkpoint.is_stale(2, "hash-at-2")


@pytest.mark.parametrize("sequence, source_hash", [(3, "hash-at-2"), (2, "hash-at-3")])
def test_checkpoint_is_stale_when_sequence_or_hash_changes(sequence, source_hash):
    checkpoint = checkpoint_projection(stream(), source_hash="hash-at-2")

    assert checkpoint.is_stale(sequence, source_hash)
    with pytest.raises(IntegrityFailure, match="stale"):
        checkpoint.require_fresh(sequence, source_hash)


def test_checkpoint_projection_uses_projection_last_sequence():
    checkpoint = checkpoint_projection(stream(), source_hash="source-digest")

    assert checkpoint.source_sequence == 2
    assert checkpoint.projection.event_count == 2


def test_checkpoint_rejects_inconsistent_source_and_invalid_metadata():
    projection = project_session(stream())
    with pytest.raises(IntegrityFailure):
        create_projection_checkpoint(projection, source_sequence=1, source_hash="hash")
    with pytest.raises(InvalidInput):
        create_projection_checkpoint(projection, source_sequence=2, source_hash="")
    with pytest.raises(InvalidInput):
        create_projection_checkpoint(projection, source_sequence=2, source_hash="hash", projection_version=0)


def test_checkpoint_is_immutable():
    checkpoint = checkpoint_projection(stream(), source_hash="source-digest")

    with pytest.raises(AttributeError):
        checkpoint.source_hash = "changed"


def test_checkpoint_type_is_explicit():
    assert ProjectionCheckpoint.__name__ == "ProjectionCheckpoint"
