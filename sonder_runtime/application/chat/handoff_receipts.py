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
    # Host classifier reason for the lane choice; never user or model text.
    routing_reason: str = ""
    # Opaque id of the served work run holding the durable answer, when the
    # turn was executed as a bounded HTTP work run.
    work_run_id: str = ""

    def public_receipt(self) -> dict[str, str]:
        fields = {
            "status": self.status,
            "work_run_id": self.work_run_id,
            "requested_mode": self.requested_mode,
            "routing_reason": self.routing_reason,
            "session_ref": self.session_ref,
            "admission_event_id": self.admission_event_id,
            "return_event_id": self.return_event_id,
            "source_event_id": self.source_event_id,
        }
        receipt = {key: value for key, value in fields.items() if value}
        if self.work_run_id:
            # The routes a client uses to fetch or stop the run; the chat
            # text itself stays client-neutral.
            receipt["get_url"] = "/v1/work-runs/%s" % self.work_run_id
            receipt["cancel_url"] = "/v1/work-runs/%s/cancel" % self.work_run_id
        return receipt


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
        source: ChatSourceEvent | None, *, routing_reason: str = "",
    ) -> SessionEvent:
        if not isinstance(routing_reason, str) or len(routing_reason) > 240:
            raise ValueError("routing_reason must be bounded text")
        provenance = handoff.provenance
        return self._repository.append(session_id, "chat.work.admitted", {
            "version": 1,
            "requested_mode": handoff.requested_mode,
            # The lane decision's host reason keeps the admitted route
            # explainable from the durable stream without any request text.
            "routing_reason": routing_reason,
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
