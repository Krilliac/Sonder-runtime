"""Provider-neutral artifact and spill ports (WP3 SEAM-012).

Handles are bounded capabilities, not paths or ownership transfers. An
adapter owns bytes and cleanup; callers receive immutable metadata and may
release a handle without taking ownership of the store.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class SpillState(str, Enum):
    OPEN = "open"
    COMMITTED = "committed"
    ABORTED = "aborted"
    EXPIRED = "expired"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class ArtifactHandle:
    """Immutable, bounded reference to an artifact owned by a store."""

    artifact_id: str
    size_bytes: int
    sha256: str
    media_type: str = "application/octet-stream"
    name: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_id, str) or not self.artifact_id.strip():
            raise ValueError("artifact_id must be non-empty")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        if not isinstance(self.sha256, str) or len(self.sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.sha256.lower()
        ):
            raise ValueError("sha256 must be a hexadecimal SHA-256 digest")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("media_type must be non-empty")
        if self.name is not None and not isinstance(self.name, str):
            raise ValueError("name must be a string or None")


@dataclass(frozen=True, slots=True)
class SpillSpec:
    """Limits and metadata for one staged large output."""

    max_bytes: int
    media_type: str = "application/octet-stream"
    name: str | None = None
    ttl_seconds: float | None = None

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("media_type must be non-empty")
        if self.name is not None and not isinstance(self.name, str):
            raise ValueError("name must be a string or None")
        if self.ttl_seconds is not None and (
            type(self.ttl_seconds) not in (int, float) or self.ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be positive or None")


@dataclass(frozen=True, slots=True)
class SpillSnapshot:
    spill_id: str
    state: SpillState
    size_bytes: int
    max_bytes: int
    artifact: ArtifactHandle | None = None


class AttachmentStore(Protocol):
    """Store and retrieve already-bounded, immutable artifact attachments."""

    # [any thread, async safe] The store retains ownership of the payload.
    def put(self, data: bytes, *, media_type: str, name: str | None = None) -> ArtifactHandle: ...

    # [any thread, async safe] Returns at most ``max_bytes`` from the artifact.
    def read(self, handle: ArtifactHandle, *, max_bytes: int) -> bytes: ...

    # [any thread, thread-safe] Idempotent best-effort release of one artifact.
    def release(self, handle: ArtifactHandle) -> None: ...


class SpillHandle(Protocol):
    """Non-owning write capability for one store-owned spill transaction."""

    @property
    def spill_id(self) -> str: ...

    # [any thread, thread-safe] Returns the current bounded lifecycle state.
    def snapshot(self) -> SpillSnapshot: ...

    # [any thread, async safe] Rejects writes that exceed SpillSpec.max_bytes.
    def write(self, chunk: bytes) -> int: ...

    # [any thread, async safe] Seals bytes and returns an immutable artifact.
    def commit(self) -> ArtifactHandle: ...

    # [any thread, thread-safe] Idempotent discard; never returns an artifact.
    def abort(self) -> None: ...

    # [any thread, thread-safe] Releases this capability; store cleanup remains authoritative.
    def close(self) -> None: ...


class SpillStore(Protocol):
    """Lifecycle owner for bounded staging of large outputs."""

    # [any thread, async safe] Creates an OPEN spill owned by this store.
    def begin(self, spec: SpillSpec) -> SpillHandle: ...

    # [any thread, thread-safe] Expires/discards abandoned spills by policy.
    def reap(self) -> int: ...


__all__ = [
    "ArtifactHandle", "AttachmentStore", "SpillHandle", "SpillSnapshot",
    "SpillSpec", "SpillState", "SpillStore",
]
