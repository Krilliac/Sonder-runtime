"""Critical-history retention rules for session compaction (issue #510 §1).

A compaction summary replaces a source range in provider-facing context, so
anything it silently drops is lost to the continuing conversation even though
the append-only source events still exist.  This module defines, without any
I/O, which source events carry *critical* history and how a summary must
represent them:

* failures -- ``*.failed``/``*.error`` events and events that report a
  failed/denied/cancelled/timeout status, ``ok``/``success`` false, a non-zero
  (integer or numeric-string) exit code, an error, or a Python traceback on
  stderr, either at the top level or exactly one level inside
  ``result``/``output``/``receipt``/``response`` (the shapes lane receipts and
  session capture store);
* constraints and requirements -- ``constraints``/``requirements`` payload
  fields, including on plain text messages that are otherwise collapsed;
* decisions, facts, unresolved tasks, artifacts, and tool outcomes -- the
  structured summary fields.

Bulky tool output is the first thing to leave live context: a large tool
result is represented by a digest-bound reference (flat ``reference_*`` keys)
instead of being inlined in the summary.  The reference is *not* content-free:
beside it the summary keeps the event's call/status keys and, for a failure,
its error, constraint, and stderr/traceback values, each bounded to
``MAX_RETAINED_KEY_BYTES`` (stderr keeps its tail, where a traceback's cause
is).  Bulk payload fields (output, content, logs) are never copied.  The full
payload is recoverable because the source event remains append-only.

``critical_retention_problems`` is a deterministic gate, independent of the
summarizing engine, so a lossy (for example model-backed) engine cannot drop a
failure or constraint while reporting itself valid.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json

from .ports.compaction import CompactionSummary, SessionHistoryEvent


SUMMARY_SCHEMA_VERSION = 2
"""Top-level ``summary_schema`` of compaction events written by this engine."""

INLINE_TOOL_OUTPUT_BYTES = 2 * 1024
"""Tool payloads above this canonical size are summarized by reference."""

MAX_RETAINED_KEY_BYTES = 1024
"""Bound for one critical value carried beside a bulky-output reference."""

STRUCTURED_FIELDS = (
    "facts", "decisions", "unresolved_tasks", "artifacts", "tool_outcomes",
)
CONSTRAINT_FIELDS = ("constraints", "requirements")
FAILURE_FIELDS = ("error", "error_code", "failure", "failures", "failed_attempts")
STATUS_FIELDS = (
    "call_id", "tool", "name", "status", "ok", "success", "exit_code", "returncode",
)
STDERR_FIELDS = ("stderr", "traceback")
NESTED_RESULT_FIELDS = ("result", "output", "receipt", "response")
"""Containers inspected exactly one level deep for failure signals."""
TOOL_OUTPUT_TYPES = frozenset({"tool.result", "tool.completed"})
MESSAGE_TYPES = frozenset({"message.received", "message.emitted", "message.sent"})
"""Conversation text types; ``message.emitted`` is the canonical assistant type."""
_FAILED_STATUSES = frozenset({
    "failed", "failure", "error", "errored", "timeout", "timed_out", "timedout",
    "cancelled", "canceled", "denied", "rejected", "aborted", "killed",
})
_TRACEBACK_MARK = "Traceback (most recent call last)"
_MAX_EXIT_CODE_TEXT = 12
REFERENCE_KEYS = (
    "reference_event_id", "reference_sequence", "reference_event_type",
    "reference_sha256", "reference_byte_count",
)
_TRUNCATION_MARK = "...[truncated; recover full value from the source reference]"


def canonical_bytes(value: object) -> bytes:
    """Canonical JSON used for digests (matches the context archive)."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False, default=str,
    ).encode("utf-8")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, frozenset):
        return sorted((_plain(item) for item in value), key=str)
    return value


def payload_digest(payload: Mapping[str, object]) -> tuple[str, int]:
    encoded = canonical_bytes(_plain(dict(payload)))
    return hashlib.sha256(encoded).hexdigest(), len(encoded)


def _present(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (tuple, list, Mapping, set, frozenset)):
        return bool(value)
    return True


def _nonzero_exit(code: object) -> bool:
    if isinstance(code, bool):
        return False
    if isinstance(code, int):
        return code != 0
    if isinstance(code, str):
        text = code.strip()
        digits = text[1:] if text[:1] in ("+", "-") else text
        if digits.isdigit() and len(text) <= _MAX_EXIT_CODE_TEXT:
            return int(text) != 0
    return False


def _has_failure_signal(payload: Mapping[str, object]) -> bool:
    """Failure markers within one mapping level (no recursion)."""
    if any(_present(payload.get(key)) for key in FAILURE_FIELDS):
        return True
    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in _FAILED_STATUSES:
        return True
    if payload.get("ok") is False or payload.get("success") is False:
        return True
    if any(_nonzero_exit(payload.get(key)) for key in ("exit_code", "returncode")):
        return True
    if _present(payload.get("traceback")):
        return True
    stderr = payload.get("stderr")
    return isinstance(stderr, str) and _TRACEBACK_MARK in stderr


def _failed_containers(payload: Mapping[str, object]) -> tuple[str, ...]:
    return tuple(
        key for key in NESTED_RESULT_FIELDS
        if isinstance(payload.get(key), Mapping) and _has_failure_signal(payload[key])
    )


def is_failure(event: SessionHistoryEvent) -> bool:
    """Whether the event records a failed attempt that must survive."""
    if event.event_type.endswith((".failed", ".error", ".errored")):
        return True
    return _has_failure_signal(event.payload) or bool(_failed_containers(event.payload))


_FAILURE_DETAIL_FIELDS = (*FAILURE_FIELDS, *STATUS_FIELDS, *STDERR_FIELDS)


def critical_keys(event: SessionHistoryEvent) -> tuple[str, ...]:
    """Payload keys whose values a summary must carry for this event.

    Nested failure details are named ``<container>.<key>`` (for example
    ``result.status``): a fully retained event carries them inside its
    container, a bulky-output reference carries them as flat keys.
    """
    payload = event.payload
    keys = [key for key in CONSTRAINT_FIELDS if _present(payload.get(key))]
    if is_failure(event):
        keys.extend(
            key for key in _FAILURE_DETAIL_FIELDS
            if key in payload and payload.get(key) is not None
        )
        for container in _failed_containers(payload):
            nested = payload[container]
            keys.extend(
                f"{container}.{key}" for key in _FAILURE_DETAIL_FIELDS
                if key in nested and nested.get(key) is not None
            )
    return tuple(dict.fromkeys(keys))


def _critical_value(payload: Mapping[str, object], key: str) -> tuple[bool, object]:
    """Resolve a plain or ``container.key`` critical key in a payload."""
    if key in payload:
        return True, payload[key]
    container, _, inner = key.partition(".")
    nested = payload.get(container) if inner else None
    if isinstance(nested, Mapping) and inner in nested:
        return True, nested[inner]
    return False, None


def is_critical(event: SessionHistoryEvent) -> bool:
    return is_failure(event) or bool(critical_keys(event))


def _truncate(text: str, *, tail: bool) -> str:
    budget = MAX_RETAINED_KEY_BYTES - len(_TRUNCATION_MARK.encode("utf-8"))
    encoded = text.encode("utf-8")
    if tail:
        return _TRUNCATION_MARK + encoded[-budget:].decode("utf-8", errors="ignore")
    return encoded[:budget].decode("utf-8", errors="ignore") + _TRUNCATION_MARK


def _bounded(value: object, *, tail: bool = False) -> object:
    """A flat, bounded copy of one critical value kept beside a reference.

    Reference payloads stay flat (scalars only) so every replay surface can
    serialize them; a structured value is carried as its canonical JSON text.
    ``tail`` keeps the end of an oversized value (stderr and tracebacks).
    """
    plain = _plain(value)
    if isinstance(plain, (bool, int, float)) or plain is None:
        return plain
    text = plain if isinstance(plain, str) else canonical_bytes(plain).decode("utf-8")
    if len(text.encode("utf-8")) <= MAX_RETAINED_KEY_BYTES:
        return text
    return _truncate(text, tail=tail)


def _bounded_for(key: str, value: object) -> object:
    return _bounded(value, tail=key.rpartition(".")[2] in STDERR_FIELDS)


def reference_payload(event: SessionHistoryEvent) -> dict[str, object]:
    """Digest-bound pointer plus bounded status and critical values.

    Not content-free: error, constraint, and (for failures) stderr values are
    kept, each at most ``MAX_RETAINED_KEY_BYTES``; bulk fields are not.
    """
    digest, byte_count = payload_digest(event.payload)
    retained: dict[str, object] = {
        "reference_event_id": event.event_id,
        "reference_sequence": event.sequence,
        "reference_event_type": event.event_type,
        "reference_sha256": digest,
        "reference_byte_count": byte_count,
    }
    for key in (*STATUS_FIELDS, *FAILURE_FIELDS, *CONSTRAINT_FIELDS):
        if key in event.payload and event.payload.get(key) is not None:
            retained[key] = _bounded(event.payload[key])
    for key in critical_keys(event):
        found, value = _critical_value(event.payload, key)
        if found and key not in retained:
            retained[key] = _bounded_for(key, value)
    return retained


def summarized_modality(event: SessionHistoryEvent) -> SessionHistoryEvent | None:
    """Return the typed modality a v2 summary carries for ``event``.

    ``None`` means the event is plain live conversation text with no critical
    content; its words stay recoverable through the bound source range.
    """
    if event.event_type in MESSAGE_TYPES and event.modality == "text":
        if not is_critical(event):
            return None
        return event
    if event.event_type in TOOL_OUTPUT_TYPES:
        _, byte_count = payload_digest(event.payload)
        if byte_count > INLINE_TOOL_OUTPUT_BYTES:
            return SessionHistoryEvent(
                event.event_id, event.session_id, event.sequence,
                event.event_type, reference_payload(event), event.modality,
            )
    return event


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def critical_retention_problems(
    events: Iterable[SessionHistoryEvent], summary: CompactionSummary,
) -> tuple[str, ...]:
    """List every critical source item the summary fails to carry.

    An empty tuple means every structured value, failure, constraint, and
    requirement in ``events`` is represented in ``summary`` (directly or by a
    digest-bound reference whose critical values are kept verbatim or visibly
    truncated).
    """
    problems: list[str] = []
    modalities = {item.event_id: item for item in summary.modalities}
    for event in events:
        for field in STRUCTURED_FIELDS:
            carried = set(getattr(summary, field))
            for value in _strings(event.payload.get(field)):
                if value not in carried:
                    problems.append(f"{event.event_id}: {field} value omitted")
        keys = critical_keys(event)
        if not keys and not is_failure(event):
            continue
        retained = modalities.get(event.event_id)
        if retained is None:
            kind = "failure" if is_failure(event) else "constraint"
            problems.append(f"{event.event_id}: {kind} event omitted")
            continue
        if retained.event_type != event.event_type:
            problems.append(f"{event.event_id}: retained event type changed")
            continue
        payload = retained.payload
        is_reference = "reference_sha256" in payload
        for key in keys:
            _, source_value = _critical_value(event.payload, key)
            if is_reference:
                # References carry nested details as flat ``container.key``.
                if key not in payload:
                    problems.append(f"{event.event_id}: critical key {key} omitted")
                    continue
                actual = _plain(payload[key])
                if actual == _plain(source_value) or actual == _bounded_for(key, source_value):
                    continue
                problems.append(f"{event.event_id}: critical key {key} changed")
                continue
            found, actual = _critical_value(payload, key)
            if not found:
                problems.append(f"{event.event_id}: critical key {key} omitted")
            elif _plain(actual) != _plain(source_value):
                problems.append(f"{event.event_id}: critical key {key} changed")
        if is_reference:
            digest, byte_count = payload_digest(event.payload)
            if (
                payload.get("reference_event_id") != event.event_id
                or payload.get("reference_sequence") != event.sequence
                or payload.get("reference_event_type") != event.event_type
                or payload.get("reference_sha256") != digest
                or payload.get("reference_byte_count") != byte_count
            ):
                problems.append(f"{event.event_id}: source reference does not match")
    return tuple(dict.fromkeys(problems))


__all__ = [
    "INLINE_TOOL_OUTPUT_BYTES", "MAX_RETAINED_KEY_BYTES", "SUMMARY_SCHEMA_VERSION",
    "REFERENCE_KEYS", "canonical_bytes", "critical_keys", "critical_retention_problems",
    "is_critical", "is_failure", "payload_digest", "reference_payload",
    "summarized_modality",
]
