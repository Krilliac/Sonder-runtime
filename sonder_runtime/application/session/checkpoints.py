"""Versioned, source-pinned session projection checkpoints (WP2 SESSION-009).

Checkpoints are application values only.  They never persist themselves and
never replay or inspect a repository.  The source sequence and hash make it
safe for an adapter or caller to reject a checkpoint after the event stream
has advanced or changed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Mapping
from typing import Iterable

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...domain.common.events import DomainEvent
from .projections import SessionProjection, project_session


@dataclass(frozen=True, slots=True)
class ProjectionCheckpoint:
    """An immutable projection identified by the exact source it consumed."""

    session_id: str
    projection_version: int
    source_sequence: int
    source_hash: str
    projection: SessionProjection | Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise InvalidInput("session_id must be non-empty text")
        if not isinstance(self.projection, (SessionProjection, Mapping)):
            raise InvalidInput("checkpoint projection must be a projection value")
        _validate_version(self.projection_version)
        _validate_sequence(self.source_sequence)
        _validate_hash(self.source_hash)
        if isinstance(self.projection, SessionProjection) and self.projection.last_sequence != self.source_sequence:
            raise IntegrityFailure(
                "checkpoint source_sequence must match projection last_sequence"
            )

    @classmethod
    def create(
        cls,
        session_id: str,
        projection_version: int,
        source_sequence: int,
        source_hash: str,
        projection: SessionProjection | Mapping[str, object],
    ) -> "ProjectionCheckpoint":
        """Create a checkpoint from a versioned projection payload."""
        return cls(session_id, projection_version, source_sequence, source_hash, projection)

    def is_stale(self, source_sequence: int, source_hash: str) -> bool:
        """Return whether the checkpoint does not describe the supplied source."""
        _validate_sequence(source_sequence)
        _validate_hash(source_hash)
        return (self.source_sequence, self.source_hash) != (source_sequence, source_hash)

    def is_current(self, *, source_sequence: int, source_hash: str) -> bool:
        """Return whether the checkpoint still names the current source."""
        return not self.is_stale(source_sequence, source_hash)

    def digest(self) -> str:
        """Return a deterministic identity for this checkpoint value."""
        if isinstance(self.projection, SessionProjection):
            projection = self.projection.__dict__ if hasattr(self.projection, "__dict__") else {
                name: getattr(self.projection, name)
                for name in self.projection.__dataclass_fields__
            }
        else:
            projection = dict(self.projection)
        material = json.dumps(
            [self.session_id, self.projection_version, self.source_sequence, self.source_hash, projection],
            sort_keys=True, separators=(",", ":"), default=str,
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def require_fresh(self, source_sequence: int, source_hash: str) -> None:
        """Raise ``IntegrityFailure`` when the checkpoint is not source-current."""
        if self.is_stale(source_sequence, source_hash):
            raise IntegrityFailure("projection checkpoint is stale")


def _validate_version(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidInput("projection_version must be a positive integer")


def _validate_sequence(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidInput("source_sequence must be a non-negative integer")


def _validate_hash(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput("source_hash must be non-empty text")


def create_projection_checkpoint(
    projection: SessionProjection,
    *,
    source_sequence: int,
    source_hash: str,
    projection_version: int = 1,
) -> ProjectionCheckpoint:
    """Pin an already-built projection to its exact source identity."""
    return ProjectionCheckpoint(
        session_id=projection.session_id,
        projection_version=projection_version,
        source_sequence=source_sequence,
        source_hash=source_hash,
        projection=projection,
    )


def checkpoint_projection(
    events: Iterable[DomainEvent],
    *,
    source_hash: str,
    projection_version: int = 1,
) -> ProjectionCheckpoint:
    """Project a stream and pin the result to the caller-supplied source hash."""
    projection = project_session(events)
    return create_projection_checkpoint(
        projection,
        source_sequence=projection.last_sequence,
        source_hash=source_hash,
        projection_version=projection_version,
    )


__all__ = [
    "ProjectionCheckpoint",
    "checkpoint_projection",
    "create_projection_checkpoint",
]
