"""Durable session event vocabulary and validation (WP2 SESSION-003).

This module is domain-only: persistence, dispatch, and projection remain
adapter concerns. Payloads contain references and bounded metadata, not raw
prompts or secrets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
import json
import uuid


class EventValidationError(ValueError):
    """Raised when a durable event or payload is not schema-valid."""


class EventKind(str, Enum):
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    SESSION_PAUSED = "session.paused"
    SESSION_RESUMED = "session.resumed"
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_EMITTED = "message.emitted"
    MESSAGE_COMPLETED = "message.completed"
    CONTEXT_UPDATED = "context.updated"
    CONTEXT_WINDOW_CHANGED = "context.window_changed"
    PROMPT_CREATED = "prompt.created"
    PROMPT_SUBMITTED = "prompt.submitted"
    MODEL_REQUESTED = "model.requested"
    MODEL_STARTED = "model.started"
    MODEL_COMPLETED = "model.completed"
    MODEL_FAILED = "model.failed"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    GOAL_CREATED = "goal.created"
    GOAL_UPDATED = "goal.updated"
    GOAL_COMPLETED = "goal.completed"
    PLAN_CREATED = "plan.created"
    PLAN_STEP_STARTED = "plan.step_started"
    PLAN_STEP_COMPLETED = "plan.step_completed"
    COMPACTION_STARTED = "compaction.started"
    COMPACTION_COMPLETED = "compaction.completed"
    RETRIEVAL_REQUESTED = "retrieval.requested"
    RETRIEVAL_COMPLETED = "retrieval.completed"
    SUBAGENT_SPAWNED = "subagent.spawned"
    SUBAGENT_STARTED = "subagent.started"
    SUBAGENT_COMPLETED = "subagent.completed"
    SUBAGENT_FAILED = "subagent.failed"
    CANCELLATION_REQUESTED = "cancellation.requested"
    CANCELLATION_COMPLETED = "cancellation.completed"
    ERROR_RAISED = "error.raised"
    ERROR_RECOVERED = "error.recovered"
    ARTIFACT_CREATED = "artifact.created"
    ARTIFACT_UPDATED = "artifact.updated"
    ARTIFACT_ATTACHED = "artifact.attached"


@dataclass(frozen=True)
class PayloadSchema:
    required: Mapping[str, type | tuple[type, ...]] = field(default_factory=dict)
    optional: Mapping[str, type | tuple[type, ...]] = field(default_factory=dict)


_TEXT = str
_NUMBER = (int, float)
_SCHEMAS: dict[EventKind, PayloadSchema] = {}


def _schema(*required: tuple[str, type | tuple[type, ...]], optional=()) -> PayloadSchema:
    return PayloadSchema(dict(required), dict(optional))


def _register(kinds: tuple[EventKind, ...], schema: PayloadSchema) -> None:
    for kind in kinds:
        _SCHEMAS[kind] = schema


_register((EventKind.SESSION_STARTED, EventKind.SESSION_ENDED, EventKind.SESSION_PAUSED, EventKind.SESSION_RESUMED), _schema(optional=(("reason", _TEXT), ("status", _TEXT))))
_register((EventKind.MESSAGE_RECEIVED, EventKind.MESSAGE_EMITTED), _schema(("message_id", _TEXT), ("role", _TEXT), optional=(("content_ref", _TEXT), ("part_count", int))))
_register((EventKind.MESSAGE_COMPLETED,), _schema(("message_id", _TEXT), optional=(("status", _TEXT),)))
_register((EventKind.CONTEXT_UPDATED,), _schema(("context_ref", _TEXT), optional=(("item_count", int),)))
_register((EventKind.CONTEXT_WINDOW_CHANGED,), _schema(("window_tokens", int), optional=(("previous_tokens", int),)))
_register((EventKind.PROMPT_CREATED, EventKind.PROMPT_SUBMITTED), _schema(("prompt_id", _TEXT), optional=(("template_ref", _TEXT), ("content_ref", _TEXT))))
_register((EventKind.MODEL_REQUESTED, EventKind.MODEL_STARTED), _schema(("request_id", _TEXT), ("model", _TEXT), optional=(("provider", _TEXT), ("input_tokens", int))))
_register((EventKind.MODEL_COMPLETED,), _schema(("request_id", _TEXT), ("model", _TEXT), optional=(("output_tokens", int), ("finish_reason", _TEXT))))
_register((EventKind.MODEL_FAILED,), _schema(("request_id", _TEXT), optional=(("error_code", _TEXT), ("retryable", bool))))
_register((EventKind.TOOL_REQUESTED, EventKind.TOOL_STARTED), _schema(("call_id", _TEXT), ("tool", _TEXT), optional=(("arguments_ref", _TEXT),)))
_register((EventKind.TOOL_COMPLETED,), _schema(("call_id", _TEXT), ("tool", _TEXT), optional=(("result_ref", _TEXT), ("duration_ms", _NUMBER))))
_register((EventKind.TOOL_FAILED,), _schema(("call_id", _TEXT), optional=(("error_code", _TEXT), ("retryable", bool))))
_register((EventKind.APPROVAL_REQUESTED,), _schema(("approval_id", _TEXT), ("action", _TEXT), optional=(("expires_at", _TEXT),)))
_register((EventKind.APPROVAL_GRANTED, EventKind.APPROVAL_DENIED), _schema(("approval_id", _TEXT), optional=(("actor", _TEXT), ("reason", _TEXT))))
_register((EventKind.GOAL_CREATED, EventKind.GOAL_UPDATED, EventKind.GOAL_COMPLETED), _schema(("goal_id", _TEXT), optional=(("status", _TEXT), ("title_ref", _TEXT))))
_register((EventKind.PLAN_CREATED,), _schema(("plan_id", _TEXT), optional=(("goal_id", _TEXT), ("step_count", int))))
_register((EventKind.PLAN_STEP_STARTED, EventKind.PLAN_STEP_COMPLETED), _schema(("plan_id", _TEXT), ("step_id", _TEXT), optional=(("status", _TEXT),)))
_register((EventKind.COMPACTION_STARTED,), _schema(("compaction_id", _TEXT), optional=(("context_ref", _TEXT),)))
_register((EventKind.COMPACTION_COMPLETED,), _schema(("compaction_id", _TEXT), ("summary_ref", _TEXT), optional=(("removed_items", int),)))
_register((EventKind.RETRIEVAL_REQUESTED,), _schema(("retrieval_id", _TEXT), optional=(("query_ref", _TEXT), ("limit", int))))
_register((EventKind.RETRIEVAL_COMPLETED,), _schema(("retrieval_id", _TEXT), ("result_count", int), optional=(("results_ref", _TEXT),)))
_register((EventKind.SUBAGENT_SPAWNED, EventKind.SUBAGENT_STARTED), _schema(("subagent_id", _TEXT), optional=(("role", _TEXT), ("parent_id", _TEXT))))
_register((EventKind.SUBAGENT_COMPLETED,), _schema(("subagent_id", _TEXT), optional=(("result_ref", _TEXT),)))
_register((EventKind.SUBAGENT_FAILED,), _schema(("subagent_id", _TEXT), optional=(("error_code", _TEXT),)))
_register((EventKind.CANCELLATION_REQUESTED, EventKind.CANCELLATION_COMPLETED), _schema(("target_id", _TEXT), optional=(("reason", _TEXT),)))
_register((EventKind.ERROR_RAISED,), _schema(("error_code", _TEXT), optional=(("message_ref", _TEXT), ("retryable", bool))))
_register((EventKind.ERROR_RECOVERED,), _schema(("error_code", _TEXT), optional=(("recovery_ref", _TEXT),)))
_register((EventKind.ARTIFACT_CREATED, EventKind.ARTIFACT_UPDATED, EventKind.ARTIFACT_ATTACHED), _schema(("artifact_id", _TEXT), optional=(("artifact_type", _TEXT), ("content_ref", _TEXT), ("sha256", _TEXT))))


def payload_schema(kind: EventKind | str) -> PayloadSchema:
    try:
        resolved = kind if isinstance(kind, EventKind) else EventKind(kind)
        return _SCHEMAS[resolved]
    except (KeyError, ValueError) as exc:
        raise EventValidationError(f"unknown event kind: {kind!r}") from exc


def _json_safe(value: Any, path: str = "payload") -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise EventValidationError(f"{path} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _json_safe(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise EventValidationError(f"{path} has a non-string key")
            _json_safe(item, f"{path}.{key}")
        return
    raise EventValidationError(f"{path} contains non-JSON value {type(value).__name__}")


def validate_payload(kind: EventKind | str, payload: Mapping[str, Any]) -> dict[str, Any]:
    schema = payload_schema(kind)
    if not isinstance(payload, Mapping):
        raise EventValidationError("payload must be an object")
    data = dict(payload)
    missing = set(schema.required) - data.keys()
    unknown = set(data) - set(schema.required) - set(schema.optional)
    if missing:
        raise EventValidationError(f"missing payload fields: {', '.join(sorted(missing))}")
    if unknown:
        raise EventValidationError(f"unknown payload fields: {', '.join(sorted(unknown))}")
    for name, expected in {**schema.required, **schema.optional}.items():
        if name in data and (not isinstance(data[name], expected) or (isinstance(data[name], bool) and (expected is int or expected == _NUMBER))):
            raise EventValidationError(f"payload.{name} has invalid type")
        if name in data and isinstance(data[name], str) and not data[name].strip():
            raise EventValidationError(f"payload.{name} must not be empty")
    _json_safe(data)
    return data


@dataclass(frozen=True)
class DurableEvent:
    """Validated, JSON-serializable event record suitable for durable storage."""

    kind: EventKind
    aggregate_type: str
    aggregate_id: str
    sequence: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    session_id: str = ""
    correlation_id: str = ""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    occurred_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    schema_version: int = 1

    def __post_init__(self) -> None:
        try:
            kind = self.kind if isinstance(self.kind, EventKind) else EventKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise EventValidationError(f"unknown event kind: {self.kind!r}") from exc
        object.__setattr__(self, "kind", kind)
        if not isinstance(self.aggregate_type, str) or not self.aggregate_type.strip() or not isinstance(self.aggregate_id, str) or not self.aggregate_id.strip():
            raise EventValidationError("aggregate_type and aggregate_id are required")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 0:
            raise EventValidationError("sequence must be a non-negative integer")
        if self.schema_version != 1:
            raise EventValidationError("unsupported event schema version")
        try:
            datetime.fromisoformat(self.occurred_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise EventValidationError("occurred_at must be ISO-8601") from exc
        object.__setattr__(self, "payload", validate_payload(kind, self.payload))

    @property
    def event_type(self) -> str:
        return self.kind.value

    @property
    def id(self) -> str:
        return self.event_id

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "event_id": self.event_id, "kind": self.kind.value,
                "aggregate_type": self.aggregate_type, "aggregate_id": self.aggregate_id,
                "sequence": self.sequence, "session_id": self.session_id,
                "correlation_id": self.correlation_id, "occurred_at": self.occurred_at,
                "payload": dict(self.payload)}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DurableEvent":
        if not isinstance(value, Mapping):
            raise EventValidationError("event must be an object")
        required = {"kind", "aggregate_type", "aggregate_id", "sequence", "payload"}
        missing = required - value.keys()
        if missing:
            raise EventValidationError(f"missing event fields: {', '.join(sorted(missing))}")
        try:
            kind = EventKind(value["kind"])
        except (TypeError, ValueError) as exc:
            raise EventValidationError(f"unknown event kind: {value.get('kind')!r}") from exc
        return cls(kind=kind, aggregate_type=value["aggregate_type"],
                   aggregate_id=value["aggregate_id"], sequence=value["sequence"], payload=value["payload"],
                   session_id=value.get("session_id", ""), correlation_id=value.get("correlation_id", ""),
                   event_id=value.get("event_id", str(uuid.uuid4())),
                   occurred_at=value.get("occurred_at", datetime.now(timezone.utc).isoformat()),
                   schema_version=value.get("schema_version", 1))


# Existing SPEC-5 callers retain their untyped envelope until migrated.
@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    aggregate_type: str
    aggregate_id: str
    sequence: int
    payload: dict = field(default_factory=dict)
    correlation_id: str = ""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
