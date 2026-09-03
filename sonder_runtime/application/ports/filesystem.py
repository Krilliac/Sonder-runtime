"""Application FileSystem capability port (WP3 / SEAM-003).

This module defines the boundary only.  It intentionally contains no path
resolution, environment reads, or filesystem I/O.  Adapters own those
mechanics and translate driver failures to the domain error taxonomy before
returning across this port.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from ...domain.common.errors import (
    CapacityExceeded,
    InvalidInput,
)
from ..context import OperationContext


class ResourceKind(str, Enum):
    FILE = "file"
    DIRECTORY = "directory"


class FileSystemOperation(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    LIST = "list"
    STAT = "stat"
    MOVE = "move"


class PolicyEffect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class FileSystemResource:
    """Stable, caller-visible identity of a resource; not an open handle."""

    path: Path
    kind: ResourceKind = ResourceKind.FILE
    resource_id: str | None = None


@dataclass(frozen=True)
class FileSystemRequest:
    """Bounded operation input shared by policy and the capability adapter."""

    operation: FileSystemOperation
    resource: FileSystemResource
    destination: FileSystemResource | None = None
    content: bytes | None = None
    recursive: bool = False
    confirmed: bool = False
    dry_run: bool = False
    max_bytes: int = 256_000
    max_entries: int = 200
    expected_version: str | None = None
    # Compatibility policy inputs remain explicit at the typed boundary.  The
    # packaged adapter owns their interpretation; callers must not smuggle
    # authorization through an untyped kwargs dictionary.
    extra_roots: str = ""
    bypass: bool = False
    developer_authorized: bool = False


@dataclass(frozen=True)
class FileSystemPolicyDecision:
    effect: PolicyEffect
    reason: str
    operation: FileSystemOperation
    resource: FileSystemResource
    required_confirmation: bool = False
    max_bytes: int | None = None
    max_entries: int | None = None


@dataclass(frozen=True)
class FileSystemObservation:
    """Bounded evidence emitted for every attempted operation."""

    operation: FileSystemOperation
    resource: FileSystemResource
    effect: PolicyEffect
    succeeded: bool
    bytes_read: int = 0
    bytes_written: int = 0
    entries_returned: int = 0
    error_code: str | None = None
    version: str | None = None


@dataclass(frozen=True)
class FileSystemEntry:
    resource: FileSystemResource
    size_bytes: int | None = None
    version: str | None = None


@dataclass(frozen=True)
class FileSystemReadResult:
    content: bytes
    observation: FileSystemObservation
    # True when the file was longer than the read bound and content is a prefix.
    truncated: bool = False


@dataclass(frozen=True)
class FileSystemWriteResult:
    resource: FileSystemResource
    bytes_written: int
    version: str | None
    observation: FileSystemObservation


@dataclass(frozen=True)
class FileSystemListResult:
    entries: tuple[FileSystemEntry, ...]
    observation: FileSystemObservation


@dataclass(frozen=True)
class FileSystemStatResult:
    entry: FileSystemEntry
    observation: FileSystemObservation


@dataclass(frozen=True)
class FileSystemMutationResult:
    resource: FileSystemResource
    observation: FileSystemObservation


class FileSystemPolicy(Protocol):
    """Pure policy decision point; it must not perform the operation."""

    def decide(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemPolicyDecision: ...


class FileSystemObserver(Protocol):
    """Sink for bounded, redacted operation evidence."""

    def record(self, observation: FileSystemObservation) -> None: ...


class FileSystem(Protocol):
    """Resource-aware filesystem capability.

    [any thread, async safe] Implementations must apply policy before touching
    a resource, emit one bounded observation per attempt, and raise only the
    domain/application errors documented below.  Returned resources are
    descriptions, never adapter-owned open handles.
    """

    def read(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemReadResult: ...

    def write(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemWriteResult: ...

    def delete(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemMutationResult: ...

    def list(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemListResult: ...

    def stat(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemStatResult: ...

    def move(
        self, request: FileSystemRequest, context: OperationContext
    ) -> FileSystemMutationResult: ...


def validate_request(request: FileSystemRequest) -> None:
    """Reject malformed or unbounded requests before policy evaluation."""
    if not isinstance(request.resource.path, Path):
        raise InvalidInput("filesystem resource path must be a Path")
    if request.max_bytes < 0 or request.max_entries < 0:
        raise InvalidInput("filesystem limits must be non-negative")
    if request.content is not None and len(request.content) > request.max_bytes:
        raise CapacityExceeded("filesystem write exceeds the request byte limit")
    if request.operation is FileSystemOperation.MOVE and request.destination is None:
        raise InvalidInput("filesystem move requires a destination")
    if request.operation is not FileSystemOperation.MOVE and request.destination is not None:
        raise InvalidInput("filesystem destination is valid only for move")


__all__ = [
    "FileSystem",
    "FileSystemEntry",
    "FileSystemListResult",
    "FileSystemMutationResult",
    "FileSystemObserver",
    "FileSystemObservation",
    "FileSystemOperation",
    "FileSystemPolicy",
    "FileSystemPolicyDecision",
    "FileSystemReadResult",
    "FileSystemRequest",
    "FileSystemResource",
    "FileSystemStatResult",
    "FileSystemWriteResult",
    "PolicyEffect",
    "ResourceKind",
    "validate_request",
]
