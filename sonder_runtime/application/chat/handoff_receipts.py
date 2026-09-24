"""Content-free, owner-scoped receipts for an admitted chat-to-work handoff.

The canonical session event stream remains the only durable source. A source
pointer proves that a completed chat turn was present in the same verified
session; it does not by itself grant the work lane permission to dereference
or expose that turn's transcript.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..ports.session_repository import SessionEvent, SessionRepository
from .lanes import ChatHandoff


@dataclass(frozen=True, slots=True)
class ChatSourceEvent:
    event_id: str
    event_hash: str

    @property
    def handoff_ref(self) -> str:
        return "session-event:" + self.event_id


@dataclass(frozen=True, slots=True)
class ChatWorkResult:
    """An observed lane return, refusal, or unknown outcome; never a success claim."""

    text: str
    status: str
    requested_mode: str = ""
    session_ref: str = ""
    admission_event_id: str = ""
    return_event_id: str = ""
    source_event_id: str = ""

    def public_receipt(self) -> dict[str, str]:
        fields = {
            "status": self.status,
            "requested_mode": self.requested_mode,
            "session_ref": self.session_ref,
            "admission_event_id": self.admission_event_id,
            "return_event_id": self.return_event_id,
            "source_event_id": self.source_event_id,
        }
        return {key: value for key, value in fields.items() if value}


class ChatWorkReceiptService:
    """Read verified source history and record work admission and outcome."""

    def __init__(self, repository: SessionRepository):
        self._repository = repository

    def source(self, session_id: str) -> ChatSourceEvent | None:
        # A single read_complete transaction verifies both the hash chain and
        # its tail. Its bounded result is never projected into a receipt.
        events = self._repository.read_complete(session_id, max_events=10_000)
        admitted: set[str] = set()
        latest = None
        for event in events:
            request_id = event.payload.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                continue
            if event.event_type == "model.requested":
                admitted.add(request_id)
            elif event.event_type == "model.response" and request_id in admitted:
                latest = ChatSourceEvent(event.event_id, event.event_hash)
        return latest

    def admit(
        self, session_id: str, handoff: ChatHandoff,
        source: ChatSourceEvent | None,
    ) -> SessionEvent:
        provenance = handoff.provenance
        return self._repository.append(session_id, "chat.work.admitted", {
            "version": 1,
            "requested_mode": handoff.requested_mode,
            "objective_sha256": hashlib.sha256(handoff.objective.encode("utf-8")).hexdigest(),
            "project_sha256": hashlib.sha256(handoff.project.encode("utf-8")).hexdigest(),
            "correlation_id": provenance.correlation_id if provenance else "",
            "source_event_id": source.event_id if source else "",
            "source_event_hash": source.event_hash if source else "",
        })

    def finish(
        self, session_id: str, admission: SessionEvent, status: str,
    ) -> SessionEvent:
        if status not in {"returned", "unknown"}:
            raise ValueError("work outcome must be returned or unknown")
        return self._repository.append(session_id, "chat.work." + status, {
            "version": 1,
            "status": status,
            "admission_event_id": admission.event_id,
        })


__all__ = ["ChatSourceEvent", "ChatWorkReceiptService", "ChatWorkResult"]
