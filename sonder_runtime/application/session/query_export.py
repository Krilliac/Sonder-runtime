"""Bounded session queries and privacy-safe replay-compatible exports.

The engine is deliberately read-only.  It consumes the ``SessionRepository``
port rather than a concrete database, advances pagination by the durable
session sequence, and never constructs model state from live configuration.
"""
from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from ..ports.session_repository import (
    IntegrityReport,
    SessionEvent,
    SessionRepository,
)
from ..ports.telemetry_sink import TelemetryRedactor
from ...domain.common.events import DomainEvent


class QueryExportError(ValueError):
    """Raised when a bounded query or cursor is invalid."""


class DefaultExportRedactor:
    """Small fail-closed default redactor owned by the application boundary."""

    _PATTERNS = (
        re.compile(r"(?i)([\"']?(?:api[-_]?key|auth[-_]?secret|secret|token|password|passwd|credential)[\"']?\s*[:=]\s*)([\"']?[^\s\"',;}{]{4,}[\"']?)"),
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+:[^/@\s]+)@"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    )

    def redact(self, text: str) -> str:
        for pattern in self._PATTERNS:
            text = pattern.sub(lambda match: match.group(1) + "[REDACTED]" if match.groups else "[REDACTED]", text)
        return text


def _positive(name: str, value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise QueryExportError(f"{name} must be between 1 and {maximum}")
    return value


def _redact(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, Mapping):
        return {str(key): _redact(item, redactor) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, redactor) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, redactor) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SessionEventRecord:
    """Stable export envelope retaining every field needed for replay."""

    session_id: str
    sequence: int
    event_id: str
    event_type: str
    occurred_at_utc: str
    payload: Mapping[str, object]
    previous_hash: str | None
    event_hash: str
    redacted: bool = False

    @classmethod
    def from_event(cls, event: SessionEvent, *, redactor: TelemetryRedactor | None = None) -> "SessionEventRecord":
        if redactor is None:
            payload = dict(event.payload)
            redacted = False
        else:
            payload = _redact(event.payload, redactor)
            redacted = payload != dict(event.payload)
        return cls(event.session_id, event.sequence, event.event_id, event.event_type,
                   event.occurred_at_utc, payload, event.previous_hash,
                   event.event_hash, redacted)

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "SessionEventRecord":
        required = {"session_id", "sequence", "event_id", "event_type",
                    "occurred_at_utc", "payload", "event_hash"}
        missing = required - set(value)
        if missing:
            raise QueryExportError(f"missing export fields: {', '.join(sorted(missing))}")
        if not isinstance(value["payload"], Mapping):
            raise QueryExportError("export payload must be an object")
        return cls(
            str(value["session_id"]), int(value["sequence"]), str(value["event_id"]),
            str(value["event_type"]), str(value["occurred_at_utc"]),
            dict(value["payload"]), value.get("previous_hash"), str(value["event_hash"]),
            bool(value.get("redacted", False)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at_utc": self.occurred_at_utc,
            "payload": dict(self.payload),
            "previous_hash": self.previous_hash,
            "event_hash": self.event_hash,
            "redacted": self.redacted,
        }

    def to_domain_event(self) -> DomainEvent:
        """Convert the export envelope to the replay boundary's event shape."""
        return DomainEvent(
            event_type=self.event_type,
            aggregate_type="session",
            aggregate_id=self.session_id,
            sequence=self.sequence,
            payload=dict(self.payload),
            id=self.event_id,
            created_at=self.occurred_at_utc,
        )


@dataclass(frozen=True, slots=True)
class TranscriptRecord:
    role: str
    content: str
    event_type: str
    sequence: int
    turn_id: str = ""
    name: str = ""

    def to_dict(self) -> dict[str, object]:
        return {"role": self.role, "content": self.content, "event_type": self.event_type,
                "sequence": self.sequence, "turn_id": self.turn_id, "name": self.name}


@dataclass(frozen=True, slots=True)
class SessionQueryPage:
    records: tuple[SessionEventRecord, ...]
    next_cursor: str | None
    scanned: int
    redaction_applied: bool

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True, slots=True)
class SessionExport:
    session_id: str
    events: tuple[SessionEventRecord, ...]
    transcript: tuple[TranscriptRecord, ...]
    integrity: IntegrityReport | None
    truncated: bool = False

    def to_jsonl(self) -> str:
        """Serialize deterministic replay-compatible event lines."""
        return "".join(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")) + "\n" for event in self.events)

    def to_dict(self) -> dict[str, object]:
        return {"schema": "sonder.session-export.v1", "session_id": self.session_id,
                "events": [event.to_dict() for event in self.events],
                "transcript": [item.to_dict() for item in self.transcript],
                "truncated": self.truncated,
                "integrity_valid": None if self.integrity is None else self.integrity.valid}


class SessionQueryEngine:
    """Read-only, bounded query/export facade over a session repository."""

    _CURSOR_VERSION = 1

    def __init__(self, repository: SessionRepository, *, max_page_size: int = 100,
                 max_scan: int = 1_000, redactor: TelemetryRedactor | None = None) -> None:
        if not isinstance(max_page_size, int) or not 1 <= max_page_size <= 10_000:
            raise QueryExportError("max_page_size must be between 1 and 10000")
        if not isinstance(max_scan, int) or not max_page_size <= max_scan <= 100_000:
            raise QueryExportError("max_scan must be between max_page_size and 100000")
        self._repository = repository
        self._max_page_size = max_page_size
        self._max_scan = max_scan
        self._redactor = redactor if redactor is not None else DefaultExportRedactor()

    @staticmethod
    def _fingerprint(session_id: str, event_type: str | None, text: str | None,
                     start_sequence: int, end_sequence: int | None) -> str:
        material = json.dumps([session_id, event_type, text, start_sequence, end_sequence],
                              separators=(",", ":"), ensure_ascii=False).encode()
        return hashlib.sha256(material).hexdigest()

    def _cursor(self, *, fingerprint: str, next_sequence: int) -> str:
        body = {"v": self._CURSOR_VERSION, "f": fingerprint, "n": next_sequence}
        encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(encoded).decode().rstrip("=")

    def _decode_cursor(self, cursor: str | None, fingerprint: str) -> int:
        if cursor is None:
            return 1
        if not isinstance(cursor, str) or len(cursor) > 512:
            raise QueryExportError("invalid pagination cursor")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            body = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise QueryExportError("invalid pagination cursor") from exc
        if body.get("v") != self._CURSOR_VERSION or body.get("f") != fingerprint:
            raise QueryExportError("pagination cursor does not match query")
        next_sequence = body.get("n")
        if isinstance(next_sequence, bool) or not isinstance(next_sequence, int) or next_sequence < 1:
            raise QueryExportError("invalid pagination cursor")
        return next_sequence

    def query_events(self, session_id: str, *, event_type: str | None = None,
                     text: str | None = None, page_size: int = 100,
                     cursor: str | None = None, start_sequence: int = 1,
                     end_sequence: int | None = None) -> SessionQueryPage:
        if not isinstance(session_id, str) or not session_id.strip():
            raise QueryExportError("session_id must be non-empty")
        page_size = _positive("page_size", page_size, self._max_page_size)
        if not isinstance(start_sequence, int) or start_sequence < 1:
            raise QueryExportError("start_sequence must be positive")
        if end_sequence is not None and (not isinstance(end_sequence, int) or end_sequence < start_sequence):
            raise QueryExportError("end_sequence must be >= start_sequence")
        fingerprint = self._fingerprint(session_id, event_type, text, start_sequence, end_sequence)
        next_sequence = self._decode_cursor(cursor, fingerprint)
        if cursor is None:
            next_sequence = start_sequence
        # Scan up to the configured bound so sparse filters can prove that a
        # page is terminal without issuing an unbounded follow-up query.
        # A port implementation may enforce a lower adapter-side ceiling.  A
        # service-level bound must never turn into an adapter validation error.
        adapter_limit = getattr(self._repository, "_max_read_limit", self._max_scan)
        if isinstance(adapter_limit, bool) or not isinstance(adapter_limit, int) or adapter_limit < 1:
            adapter_limit = self._max_scan
        scan_limit = min(self._max_scan, adapter_limit)
        raw = self._repository.read_range(session_id, start_sequence=next_sequence,
                                          end_sequence=end_sequence, limit=scan_limit)
        matches = []
        for event in raw:
            if event_type is not None and event.event_type != event_type:
                continue
            if text is not None and text not in json.dumps(event.payload, ensure_ascii=False, sort_keys=True):
                continue
            matches.append(SessionEventRecord.from_event(event, redactor=self._redactor))
        selected = matches[:page_size]
        # If the page filled, the cursor must resume after the last selected
        # record, not after the end of the scan (which may contain records that
        # belong to the next page).  Sparse filters still advance past every
        # scanned non-match.
        consumed_last = (selected[-1].sequence if len(matches) > page_size
                         else raw[-1].sequence if raw else next_sequence - 1)
        has_unread = bool(raw) and (len(matches) > page_size or len(raw) == scan_limit)
        following = self._cursor(fingerprint=fingerprint, next_sequence=consumed_last + 1) if has_unread else None
        return SessionQueryPage(tuple(selected), following, len(raw), any(item.redacted for item in selected))

    def export_events(self, session_id: str, *, start_sequence: int = 1,
                      end_sequence: int | None = None, max_events: int = 1_000,
                      include_integrity: bool = True) -> SessionExport:
        max_events = _positive("max_events", max_events, self._max_scan)
        events = self._repository.read_range(session_id, start_sequence=start_sequence,
                                             end_sequence=end_sequence, limit=max_events)
        records = tuple(SessionEventRecord.from_event(event, redactor=self._redactor) for event in events)
        transcript = self._transcript(records)
        # Integrity is a property of the chain, not merely of the selected
        # slice.  Starting verification at sequence two would necessarily
        # report a false predecessor mismatch, so verify from the chain root
        # while retaining the caller's end and event bound.
        integrity = self._repository.inspect_integrity(
            session_id, start_sequence=1, end_sequence=end_sequence,
            limit=max_events,
        ) if include_integrity else None
        truncated = len(events) == max_events
        return SessionExport(session_id, records, transcript, integrity, truncated)

    def export_transcript(self, session_id: str, *, start_sequence: int = 1,
                          end_sequence: int | None = None, max_events: int = 1_000) -> tuple[TranscriptRecord, ...]:
        return self.export_events(session_id, start_sequence=start_sequence,
                                  end_sequence=end_sequence, max_events=max_events,
                                  include_integrity=False).transcript

    @staticmethod
    def _transcript(events: tuple[SessionEventRecord, ...]) -> tuple[TranscriptRecord, ...]:
        roles = {"user.message": "user", "model.response": "assistant", "tool.call": "tool",
                 "tool.result": "tool", "message.received": "user", "message.emitted": "assistant"}
        result = []
        for event in events:
            role = roles.get(event.event_type)
            if role is None or not isinstance(event.payload.get("content", ""), str):
                continue
            result.append(TranscriptRecord(role, event.payload["content"], event.event_type,
                                           event.sequence, str(event.payload.get("turn_id", "")),
                                           str(event.payload.get("name", ""))))
        return tuple(result)


__all__ = ["QueryExportError", "DefaultExportRedactor", "SessionEventRecord", "TranscriptRecord", "SessionQueryPage",
           "SessionExport", "SessionQueryEngine"]
