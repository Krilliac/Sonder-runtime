"""Small, transport-neutral HTTP result facade for durable sessions.

The actual HTTP server remains responsible for routing and authentication.  This
module only translates a bounded read/replay request into a JSON-compatible
status/body pair.  Reads and exports are redacted by ``SessionQueryEngine``;
replay verifies the complete durable chain before returning replay metadata.
"""
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from typing import Mapping

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ..ports.session_repository import SessionRepository
from .durable_replay import crash_safe_replay
from .continuity import SessionContinuityService
from .fork import ForkBoundary
from .query_export import QueryExportError, SessionQueryEngine
from .trajectory import project_trajectory


@dataclass(frozen=True, slots=True)
class HttpSessionResult:
    """The minimal result an HTTP adapter needs to serialize."""

    status_code: int
    body: Mapping[str, object]


class HttpSessionFacade:
    """Bounded, read-only session access for an HTTP boundary.

    ``read`` exposes one bounded event page.  ``replay`` verifies the entire
    committed history (within ``max_replay_events``) and returns only redacted
    transcript data plus non-sensitive replay metadata.  ``export`` exposes the
    existing replay-compatible redacted event envelope for clients that need
    durable evidence.
    """

    def __init__(self, repository: SessionRepository, *, max_page_size: int = 100,
                 max_scan: int = 1_000, max_replay_events: int = 10_000,
                 continuity: SessionContinuityService | None = None) -> None:
        if isinstance(max_replay_events, bool) or not isinstance(max_replay_events, int) or not 1 <= max_replay_events <= 100_000:
            raise ValueError("max_replay_events must be between 1 and 100000")
        self._query = SessionQueryEngine(repository, max_page_size=max_page_size,
                                          max_scan=max_scan)
        self._repository = repository
        self._max_replay_events = max_replay_events
        self._continuity = continuity or SessionContinuityService(repository, max_events=max_replay_events)

    @staticmethod
    def _ok(body: Mapping[str, object]) -> HttpSessionResult:
        return HttpSessionResult(200, body)

    @staticmethod
    def _error(status_code: int, code: str) -> HttpSessionResult:
        return HttpSessionResult(status_code, {"error": code})

    def read(self, session_id: str, *, event_type: str | None = None,
             text: str | None = None, page_size: int = 100,
             cursor: str | None = None, start_sequence: int = 1,
             end_sequence: int | None = None) -> HttpSessionResult:
        """Return one bounded, redacted event page."""
        try:
            page = self._query.query_events(
                session_id, event_type=event_type, text=text, page_size=page_size,
                cursor=cursor, start_sequence=start_sequence, end_sequence=end_sequence,
            )
        except QueryExportError:
            return self._error(400, "invalid_session_query")
        return self._ok({
            "schema": "sonder.http-session-page.v1",
            "records": [record.to_dict() for record in page.records],
            "next_cursor": page.next_cursor,
            "scanned": page.scanned,
            "redaction_applied": page.redaction_applied,
        })

    def export(self, session_id: str, *, start_sequence: int = 1,
               end_sequence: int | None = None,
               max_events: int = 1_000) -> HttpSessionResult:
        """Return a bounded, redacted replay-compatible export."""
        try:
            exported = self._query.export_events(
                session_id, start_sequence=start_sequence, end_sequence=end_sequence,
                max_events=max_events,
            )
        except QueryExportError:
            return self._error(400, "invalid_session_export")
        return self._ok({
            "schema": "sonder.http-session-export.v1",
            **exported.to_dict(),
        })

    TITLE_CHARS = 80
    _TITLE_SCAN_EVENTS = 16

    def list_sessions(self, *, limit: int = 20, after: str | None = None) -> HttpSessionResult:
        """One page of sessions, newest activity first, each with a redacted title.

        The title is the first user message of the session, through the same
        redaction as reads, collapsed to one line and cut to ``TITLE_CHARS``.
        ``after`` is the opaque ``next_cursor`` of the previous page.
        """
        lister = getattr(self._repository, "list_sessions", None)
        if not callable(lister):
            return self._error(501, "session_list_unavailable")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return self._error(400, "invalid_session_query")
        position = None
        if after:
            position = _decode_list_cursor(after)
            if position is None:
                return self._error(400, "invalid_session_query")
        try:
            rows = lister(limit=limit, after=position)
        except ValueError:
            return self._error(400, "invalid_session_query")
        sessions = []
        for row in rows:
            sessions.append({
                "id": row.session_id,
                "title": self._title(row.session_id),
                "turns": row.user_turns,
                "events": row.events,
                "created": row.first_at_utc,
                "updated": row.updated_at_utc,
            })
        next_cursor = None
        if len(rows) == limit and rows:
            last = rows[-1]
            next_cursor = _encode_list_cursor(last.updated_at_utc, last.session_id)
        return self._ok({
            "schema": "sonder.http-session-list.v1",
            "sessions": sessions,
            "next_cursor": next_cursor,
        })

    def _title(self, session_id: str) -> str:
        try:
            transcript = self._query.export_transcript(session_id, max_events=self._TITLE_SCAN_EVENTS)
        except (IntegrityFailure, InvalidInput, QueryExportError, ValueError):
            return ""
        for item in transcript:
            if item.role == "user" and isinstance(item.content, str) and item.content.strip():
                text = " ".join(item.content.split())
                if len(text) > self.TITLE_CHARS:
                    text = text[: self.TITLE_CHARS - 1].rstrip() + "\u2026"
                return text
        return ""

    def replay(self, session_id: str, *, max_events: int | None = None) -> HttpSessionResult:
        """Verify and project a privacy-safe replay result."""
        bound = self._max_replay_events if max_events is None else max_events
        try:
            result = crash_safe_replay(self._repository, session_id, max_events=bound)
            # Use the export primitive for the public projection so transcript
            # content receives the same recursive redaction as normal reads.
            exported = self._query.export_events(session_id, max_events=bound)
        except (IntegrityFailure, InvalidInput, QueryExportError):
            return self._error(409 if isinstance(session_id, str) and session_id.strip() else 400,
                               "session_replay_unavailable")
        return self._ok({
            "schema": "sonder.http-session-replay.v1",
            "session_id": result.session_id,
            "crash_safe": result.crash_safe,
            "recovered_sequence": result.recovered_sequence,
            "integrity_valid": result.integrity.valid,
            "request_present": result.request is not None,
            "request_turn_id": result.request.turn_id if result.request is not None else None,
            "request_snapshot_digest": result.request.snapshot_digest if result.request is not None else None,
            "transcript": [item.to_dict() for item in exported.transcript],
        })

    def trajectory(self, session_id: str, *, max_events: int = 1_000) -> HttpSessionResult:
        """Return a bounded, redacted action/observation trajectory."""
        try:
            exported = self._query.export_events(session_id, max_events=max_events)
            if exported.truncated:
                return self._error(409, "session_trajectory_truncated")
            trajectory = project_trajectory(
                tuple(record.to_domain_event() for record in exported.events),
            )
        except (IntegrityFailure, InvalidInput, QueryExportError):
            return self._error(409, "session_trajectory_unavailable")
        return self._ok(trajectory.to_dict())

    def repair(self, session_id: str) -> HttpSessionResult:
        """Return a bounded, read-only safe-resume diagnosis."""
        try:
            plan = self._continuity.resume(session_id)
        except (IntegrityFailure, InvalidInput):
            return self._error(409, "session_repair_unavailable")
        return self._ok({
            "schema": "sonder.http-session-repair.v1",
            "session_id": plan.diagnosis.session_id,
            "disposition": plan.diagnosis.disposition,
            "can_resume": plan.diagnosis.can_resume,
            "valid_boundary": plan.valid_boundary,
            "resume_sequence": plan.resume_sequence,
            "checked_events": plan.diagnosis.checked_events,
            "issues": [{"sequence": issue.sequence, "code": issue.code, "detail": issue.detail}
                       for issue in plan.diagnosis.issues],
        })

    def fork(self, session_id: str, *, fork_sequence: int,
             child_session_id: str | None = None) -> HttpSessionResult:
        """Return a bounded, read-only fork plan; materialization stays caller-owned."""
        try:
            child = None
            if child_session_id is not None:
                from ...domain.common.ids import SessionId
                child = SessionId.from_serialized(child_session_id)
            plan = self._continuity.fork(
                session_id, ForkBoundary(fork_sequence), child_session_id=child,
            )
        except (IntegrityFailure, InvalidInput, ValueError):
            return self._error(409, "session_fork_unavailable")
        lineage = plan.lineage
        return self._ok({
            "schema": "sonder.http-session-fork.v1",
            "parent_session_id": str(lineage.parent_session_id),
            "child_session_id": plan.lineage.session_id,
            "boundary_sequence": lineage.boundary_sequence,
            "boundary_event_id": lineage.boundary_event_id,
            "inherited_event_count": len(plan.inherited_events),
            "next_sequence": plan.next_sequence,
        })

    def checkpoint(self, session_id: str) -> HttpSessionResult:
        """Return the current durable checkpoint without creating one."""
        try:
            checkpoint = self._continuity.load_checkpoint(session_id)
        except (IntegrityFailure, InvalidInput):
            return self._error(409, "session_checkpoint_unavailable")
        if checkpoint is None:
            return self._error(404, "session_checkpoint_not_found")
        return self._ok({
            "schema": "sonder.http-session-checkpoint.v1",
            "session_id": checkpoint.session_id,
            "projection_version": checkpoint.projection_version,
            "source_sequence": checkpoint.source_sequence,
            "source_hash": checkpoint.source_hash,
            "checkpoint_digest": checkpoint.digest(),
            "projection": dict(checkpoint.projection) if isinstance(checkpoint.projection, Mapping)
            else {name: getattr(checkpoint.projection, name) for name in checkpoint.projection.__dataclass_fields__},
        })


def _encode_list_cursor(updated: str, session_id: str) -> str:
    raw = json.dumps([updated, session_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_list_cursor(cursor: str) -> tuple[str, str] | None:
    if not isinstance(cursor, str) or len(cursor) > 2048:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        value = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if (not isinstance(value, list) or len(value) != 2
            or not all(isinstance(part, str) and part for part in value)):
        return None
    return value[0], value[1]


__all__ = ["HttpSessionResult", "HttpSessionFacade"]
