"""Full-conversation transcript projected from the session event log.

``GET /v1/sessions/<id>/transcript`` answers "what was said in this chat" for a
local assistant.  It reads the durable ``session_event`` stream (not a live
history window), so every ``user.message`` / ``model.response`` appears in
sequence order.  Each message carries the tier requested for its turn
(``model.requested``), the model that actually answered (``provider.responded``)
and any failure (``model.failed`` ``error_code``).

The builder consumes already-redacted :class:`SessionEventRecord` values, so it
adds no new disclosure.  Output is bounded by ``max_bytes``; when the budget is
spent the remainder is dropped and ``truncated`` is set.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from .query_export import SessionEventRecord, is_retention_withheld

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
# Bytes charged per message on top of its text (labels, metadata, markup).
_MESSAGE_OVERHEAD = 512
_USER_EVENTS = frozenset({"user.message", "message.received"})
_ASSISTANT_EVENTS = frozenset({"model.response", "message.emitted"})


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _clip(text: str, budget: int) -> tuple[str, bool]:
    """Cut ``text`` to at most ``budget`` UTF-8 bytes on a character boundary."""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text, False
    return raw[: max(budget, 0)].decode("utf-8", "ignore"), True


class TranscriptBuilder:
    """Accumulate transcript messages event by event, within a byte budget."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._budget = max_bytes
        self._used = 0
        self._requests: dict[str, dict[str, str]] = {}
        self._turn_request: dict[str, str] = {}
        self._answered_by: dict[str, str] = {}
        self.messages: list[dict[str, object]] = []
        self.truncated = False
        self.events_seen = 0

    @property
    def full(self) -> bool:
        return self.truncated

    def add(self, record: SessionEventRecord) -> None:
        """Fold one event (in ascending sequence order) into the transcript."""
        if self.truncated:
            return
        self.events_seen += 1
        kind, payload = record.event_type, record.payload
        request_id = _text(payload, "request_id")
        turn_id = _text(payload, "turn_id")
        if kind == "model.requested":
            if request_id:
                self._requests[request_id] = {"tier": _text(payload, "tier"),
                                              "model": _text(payload, "model")}
                if turn_id:
                    self._turn_request[turn_id] = request_id
            return
        if not request_id and turn_id:
            request_id = self._turn_request.get(turn_id, "")
        if kind == "provider.responded":
            response = payload.get("response")
            model = _text(response, "model") if isinstance(response, Mapping) else ""
            if request_id and model:
                self._answered_by[request_id] = model
            return
        if kind in _USER_EVENTS:
            self._message(record, "user", request_id, turn_id)
        elif kind in _ASSISTANT_EVENTS:
            self._message(record, "assistant", request_id, turn_id)
        elif kind == "model.failed":
            self._message(record, "failure", request_id, turn_id)

    def _message(self, record: SessionEventRecord, role: str, request_id: str,
                 turn_id: str) -> None:
        payload = record.payload
        if role == "failure":
            content = ""
        elif record.redacted and is_retention_withheld(payload):
            content = "[redacted: %s]" % (payload.get("privacy_class") or "withheld")
        else:
            raw = payload.get("content")
            if not isinstance(raw, str):
                return
            content = raw
        remaining = self._budget - self._used - _MESSAGE_OVERHEAD
        if remaining <= 0:
            self.truncated = True
            return
        content, clipped = _clip(content, remaining)
        request = self._requests.get(request_id, {})
        message: dict[str, object] = {
            "sequence": record.sequence,
            "at": record.occurred_at_utc,
            "role": role,
            "turn_id": turn_id,
            "request_id": request_id,
            "tier": request.get("tier", ""),
            "model": self._answered_by.get(request_id) or request.get("model", ""),
            "content": content,
        }
        if role == "failure":
            message["error_code"] = _text(payload, "error_code") or "MODEL_FAILED"
        self.messages.append(message)
        self._used += len(content.encode("utf-8")) + _MESSAGE_OVERHEAD
        if clipped:
            self.truncated = True


def build_transcript(records: Iterable[SessionEventRecord],
                     max_bytes: int = DEFAULT_MAX_BYTES) -> TranscriptBuilder:
    """Project ``records`` (ascending sequence) into a bounded transcript."""
    builder = TranscriptBuilder(max_bytes)
    for record in records:
        builder.add(record)
        if builder.full:
            break
    return builder


def render_markdown(session_id: str, messages: list[dict[str, object]], *,
                    truncated: bool, max_bytes: int = DEFAULT_MAX_BYTES) -> str:
    """Render messages as Markdown, one heading per message."""
    lines = ["# Chat transcript: %s" % session_id, "",
             "Messages: %d (from the durable session event log)" % len(messages), ""]
    for message in messages:
        role = str(message["role"])
        bits = ["#%s" % message["sequence"], str(message["at"])]
        if message.get("tier"):
            bits.append("tier=%s" % message["tier"])
        if message.get("model"):
            bits.append("model=%s" % message["model"])
        if role == "failure":
            lines.append("## FAILED (%s) | %s" % (message["error_code"], " | ".join(bits)))
        else:
            lines.append("## %s | %s" % (role.upper(), " | ".join(bits)))
        lines.append("")
        if role != "failure":
            lines.append(str(message["content"]))
            lines.append("")
    if truncated:
        lines.append("[transcript truncated at the %d byte limit; later messages are not shown]"
                     % max_bytes)
        lines.append("")
    return "\n".join(lines)


__all__ = ["DEFAULT_MAX_BYTES", "TranscriptBuilder", "build_transcript", "render_markdown"]
