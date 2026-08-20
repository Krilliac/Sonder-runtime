"""Application port for durable, ordered session event history."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class SessionEvent:
    """An immutable event assigned a monotonic sequence within one session."""

    session_id: str
    sequence: int
    event_id: str
    event_type: str
    occurred_at_utc: str
    payload: Mapping[str, object]
    previous_hash: str | None
    event_hash: str


@dataclass(frozen=True)
class IntegrityIssue:
    sequence: int | None
    code: str
    detail: str


@dataclass(frozen=True)
class IntegrityReport:
    session_id: str
    checked_events: int
    first_sequence: int | None
    last_sequence: int | None
    valid: bool
    issues: tuple[IntegrityIssue, ...]


class SessionRepository(Protocol):
    """The source-of-truth port for a session's append-only event stream."""

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, object],
        *,
        event_id: str | None = None,
        occurred_at_utc: str | None = None,
    ) -> SessionEvent: ...

    def read_range(
        self,
        session_id: str,
        *,
        start_sequence: int = 1,
        end_sequence: int | None = None,
        limit: int = 1_000,
    ) -> tuple[SessionEvent, ...]: ...

    def search(
        self,
        *,
        session_id: str | None = None,
        event_type: str | None = None,
        text: str | None = None,
        limit: int | None = None,
    ) -> tuple[SessionEvent, ...]: ...

    def export(
        self,
        session_id: str,
        *,
        start_sequence: int = 1,
        end_sequence: int | None = None,
        limit: int = 1_000,
    ) -> str: ...

    def inspect_integrity(
        self,
        session_id: str,
        *,
        start_sequence: int = 1,
        end_sequence: int | None = None,
        limit: int = 10_000,
    ) -> IntegrityReport: ...
