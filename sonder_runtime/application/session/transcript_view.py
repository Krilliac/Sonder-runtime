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

import dataclasses
import json
import re
from collections.abc import Mapping

from ..ports.session_repository import SessionEvent
from .query_export import SessionEventRecord, is_retention_withheld

DEFAULT_MAX_BYTES = 2 * 1024 * 1024
# Bytes charged per message for headings and JSON structure, on top of the
# JSON-escaped length of its text and metadata.  Markdown heading literals,
# escaped newlines and the minimum 3-backtick fences come to at most 54 bytes,
# the JSON structure to at most about 150; fences longer than the minimum are
# charged exactly by ``_content_cost``.
_MESSAGE_OVERHEAD = 256
_MIN_FENCE = 3
_BACKTICK_RUN = re.compile("`+")
_META_MAX_CHARS = 128
_USER_EVENTS = frozenset({"user.message", "message.received"})
_ASSISTANT_EVENTS = frozenset({"model.response", "message.emitted"})


def _text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _escaped_len(text: str) -> int:
    """Length of ``text`` as it appears inside the served JSON string.

    The server serialises with ``ensure_ascii=True``, so control characters
    and non-ASCII text grow up to six-fold; the cap is charged on that size.
    """
    return len(json.dumps(text)) - 2


def _fence_len(text: str) -> int:
    """Backticks in the fence around ``text``: longer than any run it holds."""
    return max(_MIN_FENCE, max((len(run) for run in _BACKTICK_RUN.findall(text)), default=0) + 1)


def _content_cost(text: str, markdown: bool) -> int:
    """Bytes ``text`` costs in the served body: its JSON-escaped length plus,
    for Markdown, the two fences beyond the minimum (the minimum is in the overhead)."""
    cost = _escaped_len(text)
    if markdown:
        cost += 2 * (_fence_len(text) - _MIN_FENCE)
    return cost


def _one_line(value: str) -> str:
    """A bounded single-line metadata value (it is shown in a heading)."""
    return " ".join(value.split())[:_META_MAX_CHARS]


def _clip(text: str, budget: int, *, markdown: bool = False) -> tuple[str, bool]:
    """Cut ``text`` so its served cost (``_content_cost``) is at most ``budget``."""
    if _content_cost(text, markdown) <= budget:
        return text, False
    low, high = 0, min(len(text), budget)
    while low < high:
        mid = (low + high + 1) // 2
        if _content_cost(text[:mid], markdown) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low], True


class TranscriptBuilder:
    """Accumulate transcript messages event by event, within a byte budget."""

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES, *, markdown: bool = False) -> None:
        self._budget = max_bytes
        self._markdown = markdown
        self._used = 0
        self._requests: dict[str, dict[str, str]] = {}
        self._turn_request: dict[str, str] = {}
        self._answered_by: dict[str, str] = {}
        self.messages: list[dict[str, object]] = []
        self.truncated = False
        self.truncated_reason = ""
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
        request = self._requests.get(request_id, {})
        message: dict[str, object] = {
            "sequence": record.sequence,
            "at": _one_line(record.occurred_at_utc),
            "role": role,
            "turn_id": _one_line(turn_id),
            "request_id": _one_line(request_id),
            "tier": _one_line(request.get("tier", "")),
            "model": _one_line(self._answered_by.get(request_id)
                               or request.get("model", "")),
            "content": "",
        }
        if role == "failure":
            message["error_code"] = _one_line(_text(payload, "error_code")) or "MODEL_FAILED"
        cost = _MESSAGE_OVERHEAD + sum(
            _escaped_len(str(v)) for k, v in message.items() if k != "content")
        remaining = self._budget - self._used - cost
        if remaining <= 0:
            self.truncated = True
            self.truncated_reason = "bytes"
            return
        content, clipped = _clip(content, remaining, markdown=self._markdown)
        message["content"] = content
        self.messages.append(message)
        self._used += cost + _content_cost(content, self._markdown)
        if clipped:
            self.truncated = True
            self.truncated_reason = "bytes"


def _fenced(text: str) -> list[str]:
    """``text`` inside a code fence longer than any backtick run it holds, so
    message content can never start a heading or close its own block."""
    fence = "`" * _fence_len(text)
    return [fence, text, fence]


def render_markdown(session_id: str, messages: list[dict[str, object]], *,
                    truncated: bool, reason: str = "bytes",
                    max_bytes: int = DEFAULT_MAX_BYTES) -> str:
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
            lines.extend(_fenced(str(message["content"])))
            lines.append("")
    if truncated:
        if reason == "events":
            lines.append("[transcript truncated: the event scan limit was reached; "
                         "later messages are not shown]")
        else:
            lines.append("[transcript truncated at the %d byte limit; later messages "
                         "are not shown]" % max_bytes)
        lines.append("")
    return "\n".join(lines)


_IDS = ("request_id", "turn_id")
_PROJECTION = {"model.requested": (*_IDS, "tier", "model"),
               "provider.responded": (*_IDS, "response"),
               "model.failed": (*_IDS, "error_code"),
               **{kind: ("content", *_IDS) for kind in _USER_EVENTS | _ASSISTANT_EVENTS}}


def project_event(event: SessionEvent) -> SessionEvent | None:
    """Keep only what the builder shows; None for events it ignores.

    Runs BEFORE redaction so the redactor never walks payload the transcript
    never exposes (prompts, history, raw provider responses).
    """
    keys = _PROJECTION.get(event.event_type)
    if keys is None:
        return None
    payload = {k: event.payload[k] for k in keys if k in event.payload}
    if "response" in payload:
        model = payload["response"].get("model") if isinstance(payload["response"], Mapping) else None
        payload["response"] = {"model": model} if isinstance(model, str) else {}
    return dataclasses.replace(event, payload=payload)


__all__ = ["DEFAULT_MAX_BYTES", "TranscriptBuilder", "project_event", "render_markdown"]
