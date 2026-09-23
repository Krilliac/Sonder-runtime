"""Persistence-neutral, sealed runtime checkpoint contract.

This is a bounded foundation for issue #510.  It deliberately stores only
JSON-safe execution metadata; secrets and opaque runtime objects are rejected
at the boundary.  The SQLite adapter owns durability and compare-and-set.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Protocol


SCHEMA_VERSION = 1
MAX_CHECKPOINT_BYTES = 256 * 1024
_SENSITIVE_KEYS = frozenset({
    "secret", "password", "passwd", "private_key", "access_token",
    "refresh_token", "id_token", "api_token", "auth_token", "api_key",
    "client_secret", "authorization", "cookie",
})


class CheckpointError(ValueError):
    """Invalid or unsafe checkpoint data."""


class CheckpointConflict(CheckpointError):
    """The expected generation no longer matches durable state."""


class RestoreStatus(str, Enum):
    EMPTY = "empty"
    RESTORED = "restored"
    CORRUPT = "corrupt"
    INCOMPATIBLE = "incompatible"


def _safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise CheckpointError("checkpoint nesting exceeds limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CheckpointError("checkpoint floats must be finite")
        return value
    if isinstance(value, Mapping):
        output = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise CheckpointError("checkpoint mapping keys must be non-empty text")
            lowered = key.casefold().replace("-", "_").replace(" ", "_")
            if lowered in _SENSITIVE_KEYS:
                raise CheckpointError("checkpoint contains a secret-bearing field")
            output[key] = _safe(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth=depth + 1) for item in value]
    raise CheckpointError(f"checkpoint value is not JSON-safe: {type(value).__name__}")


def canonical_json(value: Mapping[str, Any]) -> bytes:
    """Return the one accepted deterministic checkpoint encoding."""
    try:
        encoded = json.dumps(_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CheckpointError("checkpoint cannot be canonically encoded") from exc
    if len(encoded) > MAX_CHECKPOINT_BYTES:
        raise CheckpointError("checkpoint exceeds maximum encoded size")
    return encoded


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CheckpointError(f"{name} must be bounded non-empty text")
    if any(ord(character) < 32 for character in value):
        raise CheckpointError(f"{name} contains control characters")


@dataclass(frozen=True, slots=True)
class RuntimeCheckpoint:
    run_id: str
    generation: int
    manifest: Mapping[str, Any]
    decisions: Mapping[str, Any] = field(default_factory=dict)
    memory_refs: Mapping[str, Any] = field(default_factory=dict)
    workers: Mapping[str, Any] = field(default_factory=dict)
    retry_state: Mapping[str, Any] = field(default_factory=dict)
    tool_state: Mapping[str, Any] = field(default_factory=dict)
    routing: Mapping[str, Any] = field(default_factory=dict)
    repository_state: Mapping[str, Any] = field(default_factory=dict)
    verification: Mapping[str, Any] = field(default_factory=dict)
    resume_cursor: str = ""
    checkpoint_id: str = ""
    created_at: str = ""
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_text(self.run_id, "run_id", 256)
        if type(self.generation) is not int or self.generation < 0:
            raise CheckpointError("generation must be a non-negative integer")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise CheckpointError("schema_version must be positive")
        if not isinstance(self.resume_cursor, str) or len(self.resume_cursor) > 4096:
            raise CheckpointError("resume_cursor must be bounded text")
        if any(ord(character) < 32 for character in self.resume_cursor):
            raise CheckpointError("resume_cursor contains control characters")
        for name in ("manifest", "decisions", "memory_refs", "workers", "retry_state", "tool_state", "routing", "repository_state", "verification"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise CheckpointError(f"{name} must be a mapping")
            object.__setattr__(self, name, _freeze(_safe(value)))
        if self.checkpoint_id:
            _validate_text(self.checkpoint_id, "checkpoint_id", 256)
        if self.created_at and (not isinstance(self.created_at, str) or len(self.created_at) > 128):
            raise CheckpointError("created_at must be bounded text")
        if self.schema_version != SCHEMA_VERSION:
            raise CheckpointError("unsupported checkpoint schema version")

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "run_id": self.run_id,
            "generation": self.generation, "manifest": self.manifest,
            "decisions": self.decisions, "memory_refs": self.memory_refs,
            "workers": self.workers, "retry_state": self.retry_state,
            "tool_state": self.tool_state, "routing": self.routing,
            "repository_state": self.repository_state, "verification": self.verification,
            "resume_cursor": self.resume_cursor, "checkpoint_id": self.checkpoint_id,
            "created_at": self.created_at,
        }

    def digest(self) -> str:
        return hashlib.sha256(canonical_json(self.body())).hexdigest()

    def sealed(self) -> dict[str, Any]:
        body = self.body()
        return {**body, "digest": self.digest()}


@dataclass(frozen=True, slots=True)
class RestoreResult:
    status: RestoreStatus
    checkpoint: RuntimeCheckpoint | None = None
    detail: str = ""


class RuntimeCheckpointRepository(Protocol):
    def save(self, checkpoint: RuntimeCheckpoint, *, expected_generation: int) -> RuntimeCheckpoint: ...
    def restore(self, run_id: str) -> RestoreResult: ...


__all__ = ["CheckpointConflict", "CheckpointError", "MAX_CHECKPOINT_BYTES", "RestoreResult", "RestoreStatus", "RuntimeCheckpoint", "RuntimeCheckpointRepository", "SCHEMA_VERSION", "canonical_json"]
