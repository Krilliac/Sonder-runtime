"""Provider-neutral event vocabulary shared by protocol adapters."""
from __future__ import annotations

from enum import Enum


class ProtocolEventType(str, Enum):
    """Stable low-cardinality event names; payloads remain provider-neutral."""

    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    SESSION_COMPLETED = "session.completed"
    JOB_STARTED = "job.started"
    JOB_OUTPUT = "job.output"
    JOB_COMPLETED = "job.completed"
    PROVIDER_HEALTH = "provider.health"
    CONTROL_SNAPSHOT = "control.snapshot"
    SCHEMA_UPDATED = "schema.updated"
    CONNECTION_RECONNECTED = "connection.reconnected"
    PROTOCOL_ERROR = "protocol.error"


def event_name(value: ProtocolEventType | str) -> str:
    """Normalize an event name without coupling callers to the enum."""
    name = value.value if isinstance(value, ProtocolEventType) else value
    if not isinstance(name, str) or not name.strip() or len(name) > 96:
        raise ValueError("protocol event name must be a non-empty string <= 96 characters")
    if any(character.isspace() for character in name):
        raise ValueError("protocol event name must not contain whitespace")
    return name


__all__ = ["ProtocolEventType", "event_name"]
