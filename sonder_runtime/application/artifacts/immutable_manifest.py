"""Digest-bound artifact manifests and bounded attachment metadata.

This module is intentionally storage-neutral.  It describes immutable
references and the validation rules a concrete AttachmentStore or SpillStore
must enforce: complete SHA-256 digests, deterministic manifests, bounded
range reads, and explicit retention windows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Iterable, Mapping


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _check_digest(value: str, field: str = "digest") -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value.lower()
    ):
        raise ValueError(f"{field} must be a hexadecimal SHA-256 digest")


def _check_name(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Explicit retention bounds for an attachment or spill."""

    retain_until: datetime | None = None
    max_reads: int | None = None

    def __post_init__(self) -> None:
        if self.retain_until is not None and self.retain_until.tzinfo is None:
            raise ValueError("retain_until must be timezone-aware")
        if self.max_reads is not None and (
            type(self.max_reads) is not int or self.max_reads < 1
        ):
            raise ValueError("max_reads must be a positive integer or None")

    def expired(self, *, now: datetime | None = None, reads: int = 0) -> bool:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return (self.retain_until is not None and current >= self.retain_until) or (
            self.max_reads is not None and reads >= self.max_reads
        )


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Complete immutable metadata for one stored payload."""

    artifact_id: str
    digest: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    name: str | None = None
    retention: RetentionPolicy = RetentionPolicy()

    def __post_init__(self) -> None:
        _check_name(self.artifact_id, "artifact_id")
        _check_digest(self.digest)
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        _check_name(self.media_type, "media_type")
        if self.name is not None:
            _check_name(self.name, "name")

    @classmethod
    def from_bytes(
        cls, artifact_id: str, payload: bytes, *, media_type: str = "application/octet-stream",
        name: str | None = None, retention: RetentionPolicy | None = None,
    ) -> "ArtifactRecord":
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        return cls(artifact_id, _digest_bytes(payload), len(payload), media_type, name, retention or RetentionPolicy())


@dataclass(frozen=True, slots=True)
class ImmutableReference:
    """Non-owning reference that can be verified without fetching payload bytes."""

    artifact_id: str
    digest: str
    size_bytes: int
    manifest_digest: str

    def __post_init__(self) -> None:
        _check_name(self.artifact_id, "artifact_id")
        _check_digest(self.digest)
        _check_digest(self.manifest_digest, "manifest_digest")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    """Deterministic complete inventory of artifact digests."""

    version: str
    entries: tuple[ArtifactRecord, ...]
    metadata: tuple[tuple[str, str], ...]
    digest: str

    def reference(self, artifact_id: str) -> ImmutableReference:
        for entry in self.entries:
            if entry.artifact_id == artifact_id:
                return ImmutableReference(entry.artifact_id, entry.digest, entry.size_bytes, self.digest)
        raise KeyError(artifact_id)


class ArtifactManifestBuilder:
    """Builds a stable manifest over every supplied artifact record."""

    def __init__(self, *, version: str = "1", metadata: Mapping[str, str] | None = None) -> None:
        _check_name(version, "version")
        self._version = version
        self._metadata = tuple(sorted((str(key), str(value)) for key, value in (metadata or {}).items()))

    def build(self, entries: Iterable[ArtifactRecord]) -> ArtifactManifest:
        ordered = tuple(sorted(entries, key=lambda item: item.artifact_id))
        if any(not isinstance(entry, ArtifactRecord) for entry in ordered):
            raise TypeError("entries must contain ArtifactRecord values")
        ids = [entry.artifact_id for entry in ordered]
        if len(ids) != len(set(ids)):
            raise ValueError("artifact_id values must be unique")
        material = {
            "version": self._version,
            "metadata": self._metadata,
            "entries": [
                (entry.artifact_id, entry.digest, entry.size_bytes, entry.media_type, entry.name,
                 entry.retention.retain_until.isoformat() if entry.retention.retain_until else None,
                 entry.retention.max_reads)
                for entry in ordered
            ],
        }
        canonical = json.dumps(material, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        return ArtifactManifest(self._version, ordered, self._metadata, _digest_bytes(canonical))


@dataclass(frozen=True, slots=True)
class SpillMetadata:
    """Bounded, range-readable metadata for committed spill output."""

    spill_id: str
    artifact: ArtifactRecord
    max_bytes: int
    preview_bytes: int = 0

    def __post_init__(self) -> None:
        _check_name(self.spill_id, "spill_id")
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        if self.artifact.size_bytes > self.max_bytes:
            raise ValueError("artifact exceeds spill max_bytes")
        if type(self.preview_bytes) is not int or not 0 <= self.preview_bytes <= self.artifact.size_bytes:
            raise ValueError("preview_bytes must be within artifact size")

    @property
    def range_readable(self) -> bool:
        return True


def bounded_range(payload: bytes, *, offset: int = 0, length: int = 0, max_bytes: int = 1 << 20) -> bytes:
    """Return one bounded byte range, rejecting ambiguous/unbounded requests."""
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(length) is not int or length < 0:
        raise ValueError("length must be a non-negative integer")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if length > max_bytes:
        raise ValueError("requested range exceeds max_bytes")
    return payload[offset : offset + length]


__all__ = [
    "ArtifactManifest", "ArtifactManifestBuilder", "ArtifactRecord", "ImmutableReference",
    "RetentionPolicy", "SpillMetadata", "bounded_range",
]
