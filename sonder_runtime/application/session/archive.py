"""Durable references and selective eviction for model context.

The session event stream is the archive.  This module appends only a bounded
reference event for an evicted tool result; the original event remains
append-only and can be recovered by identity after a restart.  That keeps raw
tool output out of the archive metadata (and avoids making another copy of
potentially sensitive content).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...application.ports.session_repository import SessionEvent, SessionRepository


_TOOL_RESULT_TYPES = frozenset({"tool.result", "tool.completed"})
_MAX_ITEMS = 256
_MAX_REFERENCE_BYTES = 2_000


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidInput("session context value must be bounded JSON") from exc


def _payload_bytes(event: SessionEvent) -> int:
    return len(_canonical(dict(event.payload)))


@dataclass(frozen=True, slots=True)
class ArchiveReference:
    """A searchable, content-free pointer to one durable source event."""

    archive_id: str
    session_id: str
    source_event_id: str
    source_sequence: int
    source_event_type: str
    sha256: str
    byte_count: int

    def as_payload(self) -> dict[str, object]:
        return {
            "archive_id": self.archive_id,
            "source_event_id": self.source_event_id,
            "source_sequence": self.source_sequence,
            "source_event_type": self.source_event_type,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
        }


@dataclass(frozen=True, slots=True)
class ArchivedContext:
    """The context view after bounded eviction and reference replacement."""

    session_id: str
    budget_bytes: int
    used_bytes: int
    retained_events: tuple[SessionEvent, ...]
    references: tuple[ArchiveReference, ...]
    placeholders: tuple[Mapping[str, object], ...]
    overflow: bool = False

    @property
    def evicted_event_ids(self) -> tuple[str, ...]:
        return tuple(reference.source_event_id for reference in self.references)

    @property
    def needs_compaction(self) -> bool:
        """Whether protected history still exceeds the requested budget."""
        return self.overflow


class SessionContextArchiveService:
    """Archive bulky tool output and provide restart-safe retrieval.

    Only ``tool.result``/``tool.completed`` events are eligible.  User and
    model messages, request snapshots, and failure/decision events are always
    retained, so compaction cannot erase the facts needed to continue or
    explain a failed attempt.
    """

    def __init__(
        self,
        repository: SessionRepository,
        *,
        max_items: int = _MAX_ITEMS,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if isinstance(max_items, bool) or not 1 <= max_items <= _MAX_ITEMS:
            raise InvalidInput(f"max_items must be between 1 and {_MAX_ITEMS}")
        self._repository = repository
        self._max_items = max_items
        self._event_id_factory = event_id_factory or (lambda: f"archive-{uuid4().hex}")

    def archive_tool_output(
        self,
        event: SessionEvent,
        *,
        reason: str = "context_budget",
        archive_id: str | None = None,
    ) -> ArchiveReference:
        """Append a reference for one tool result without copying its payload."""
        if event.event_type not in _TOOL_RESULT_TYPES:
            raise InvalidInput("only tool result events may be archived")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 128:
            raise InvalidInput("archive reason must be bounded text")
        encoded = _canonical(dict(event.payload))
        existing = self._existing_reference(event, encoded)
        if existing is not None:
            return existing
        reference = ArchiveReference(
            archive_id=archive_id or self._event_id_factory(),
            session_id=event.session_id,
            source_event_id=event.event_id,
            source_sequence=event.sequence,
            source_event_type=event.event_type,
            sha256=hashlib.sha256(encoded).hexdigest(),
            byte_count=len(encoded),
        )
        payload = reference.as_payload()
        payload["reason"] = reason.strip()
        if len(_canonical(payload)) > _MAX_REFERENCE_BYTES:
            raise InvalidInput("archive reference exceeds its size bound")
        self._repository.append(
            event.session_id,
            "context.archive.created",
            payload,
            event_id=reference.archive_id,
        )
        return reference

    def _existing_reference(
        self, event: SessionEvent, encoded: bytes
    ) -> ArchiveReference | None:
        """Return an already committed, verified pointer for this source."""
        matches = self._repository.search(
            session_id=event.session_id,
            event_type="context.archive.created",
            text=event.event_id,
            limit=min(self._max_items, getattr(self._repository, "_max_read_limit", self._max_items)),
        )
        digest = hashlib.sha256(encoded).hexdigest()
        for candidate in matches:
            payload = candidate.payload
            if (
                payload.get("source_event_id") != event.event_id
                or payload.get("source_sequence") != event.sequence
                or payload.get("source_event_type") != event.event_type
                or payload.get("sha256") != digest
                or payload.get("byte_count") != len(encoded)
                or not isinstance(payload.get("archive_id"), str)
            ):
                continue
            return ArchiveReference(
                archive_id=payload["archive_id"],
                session_id=event.session_id,
                source_event_id=event.event_id,
                source_sequence=event.sequence,
                source_event_type=event.event_type,
                sha256=digest,
                byte_count=len(encoded),
            )
        return None

    def prepare_context(
        self,
        session_id: str,
        events: Sequence[SessionEvent],
        *,
        budget_bytes: int,
    ) -> ArchivedContext:
        """Evict largest eligible tool outputs until the context fits.

        The returned placeholders are safe model-visible replacements.  They
        contain only an archive id and source identity; callers can retrieve
        the original output explicitly when it is relevant.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise InvalidInput("session_id must be non-empty")
        if isinstance(budget_bytes, bool) or not isinstance(budget_bytes, int) or budget_bytes < 0:
            raise InvalidInput("budget_bytes must be a non-negative integer")
        values = tuple(events)
        if len(values) > self._max_items:
            raise InvalidInput("context event count exceeds archive bound")
        if any(event.session_id != session_id for event in values):
            raise InvalidInput("context contains a different session")
        if any(left.sequence >= right.sequence for left, right in zip(values, values[1:])):
            raise InvalidInput("context events must be ordered")

        total = sum(_payload_bytes(event) for event in values)
        candidates = sorted(
            (event for event in values if event.event_type in _TOOL_RESULT_TYPES),
            key=lambda event: (-_payload_bytes(event), -event.sequence, event.event_id),
        )
        evicted: set[str] = set()
        references: list[ArchiveReference] = []
        placeholders: list[Mapping[str, object]] = []
        for event in candidates:
            if total <= budget_bytes:
                break
            # A reference is useful only when its model-visible replacement is
            # smaller than the source payload.  Generate the identity before
            # appending so a tiny result is skipped without a side effect.
            archive_id = self._event_id_factory()
            placeholder = {
                "role": "tool",
                "content": f"[tool output archived: {archive_id}]",
                "archive_id": archive_id,
                "source_event_id": event.event_id,
                "source_sequence": event.sequence,
            }
            if len(_canonical(placeholder)) >= _payload_bytes(event):
                continue
            reference = self.archive_tool_output(event, archive_id=archive_id)
            references.append(reference)
            evicted.add(event.event_id)
            # ``archive_tool_output`` may reuse an existing durable reference
            # on a repeated request.  Bind the placeholder to that returned
            # identity rather than the speculative id generated above.
            placeholder["archive_id"] = reference.archive_id
            placeholder["content"] = (
                f"[tool output archived: {reference.archive_id}]"
            )
            placeholders.append(placeholder)
            # The placeholder is model-visible context and therefore counts
            # toward the same budget as retained source payloads.  Archive
            # metadata itself is outside this budget and contains no output.
            total += len(_canonical(placeholder)) - _payload_bytes(event)
        retained = tuple(event for event in values if event.event_id not in evicted)
        retained_bytes = sum(_payload_bytes(event) for event in retained)
        placeholder_bytes = sum(len(_canonical(item)) for item in placeholders)
        used_bytes = retained_bytes + placeholder_bytes
        return ArchivedContext(
            session_id=session_id,
            budget_bytes=budget_bytes,
            used_bytes=used_bytes,
            retained_events=retained,
            references=tuple(references),
            placeholders=tuple(placeholders),
            overflow=used_bytes > budget_bytes,
        )

    # ``evict`` is the concise name used by context callers and keeps the
    # policy discoverable without duplicating the implementation.
    evict = prepare_context

    def retrieve(self, reference: ArchiveReference) -> Mapping[str, object]:
        """Recover and verify the original payload after restart."""
        if not isinstance(reference, ArchiveReference):
            raise InvalidInput("reference must be ArchiveReference")
        events = self._repository.read_range(
            reference.session_id,
            start_sequence=reference.source_sequence,
            end_sequence=reference.source_sequence,
            limit=1,
        )
        if len(events) != 1:
            raise IntegrityFailure("archived source event is unavailable")
        event = events[0]
        encoded = _canonical(dict(event.payload))
        if (event.event_id != reference.source_event_id
                or event.event_type != reference.source_event_type
                or hashlib.sha256(encoded).hexdigest() != reference.sha256):
            raise IntegrityFailure("archived source event identity or digest changed")
        return dict(event.payload)

    def search(self, session_id: str, query: str, *, limit: int = 20) -> tuple[SessionEvent, ...]:
        """Search raw source history by text, bounded by the repository."""
        if not isinstance(query, str) or not query.strip():
            raise InvalidInput("query must be non-empty")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= self._max_items:
            raise InvalidInput("limit is out of bounds")
        return self._repository.search(session_id=session_id, text=query.strip(), limit=limit)


__all__ = ["ArchiveReference", "ArchivedContext", "SessionContextArchiveService"]
