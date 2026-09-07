"""Local-only authority for sealed artifact sources.

This is deliberately separate from receiver transfer grants.  A source is
published only by an injected, in-process capability; it has no wire identity,
destination, staging path, or network dependency.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol


_SOURCE_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_RANGE_BYTES = 1024 * 1024


class MobilitySourceError(RuntimeError):
    """Stable local-source failure code with no path, payload, or proof text."""


@dataclass(frozen=True)
class SourceArtifactLimits:
    """Static bounds derived only from the source configuration."""

    max_object_bytes: int
    total_bytes: int
    ttl_seconds: int

    def __post_init__(self) -> None:
        for value, low, high in (
            (self.max_object_bytes, 1, 64 * 1024**3),
            (self.total_bytes, 1, 128 * 1024**3),
            (self.ttl_seconds, 1, 86400),
        ):
            if type(value) is not int or not low <= value <= high:
                raise MobilitySourceError("INVALID_BOUND")
        if self.max_object_bytes > self.total_bytes:
            raise MobilitySourceError("INVALID_BOUND")

    @property
    def max_range_bytes(self) -> int:
        return min(self.max_object_bytes, _MAX_RANGE_BYTES)


@dataclass(frozen=True)
class SourceAuthority:
    """A binding-issued source scope; callers never select its values."""

    scope_id: str
    limits: SourceArtifactLimits

    def __post_init__(self) -> None:
        if not isinstance(self.scope_id, str) or _DIGEST.fullmatch(self.scope_id) is None:
            raise MobilitySourceError("FORBIDDEN")
        if not isinstance(self.limits, SourceArtifactLimits):
            raise MobilitySourceError("FORBIDDEN")


@dataclass(frozen=True)
class SourceArtifactRange:
    """One verified bounded range from a sealed private source object."""

    source_artifact_id: str
    sha256: str
    size_bytes: int
    offset: int
    body: bytes
    chunk_sha256: str


class SourceStore(Protocol):
    def publish_sealed(self, stream, spec: dict, authority: SourceAuthority) -> dict: ...

    def inspect_sealed(self, source_artifact_id: str, authority: SourceAuthority) -> dict: ...

    def read_range(
        self, source_artifact_id: str, offset: int, length: int, authority: SourceAuthority
    ) -> SourceArtifactRange: ...

    def close(self) -> None: ...


def _source_id(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_ID.fullmatch(value) is None:
        raise MobilitySourceError("NOT_FOUND")
    return value


def _immutable_spec(value: object, limits: SourceArtifactLimits) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "sha256", "size_bytes", "media_type",
    }:
        raise MobilitySourceError("INVALID_SPEC")
    digest = value["sha256"]
    size = value["size_bytes"]
    media_type = value["media_type"]
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise MobilitySourceError("INVALID_SPEC")
    if type(size) is not int or not 0 <= size <= limits.max_object_bytes:
        raise MobilitySourceError("INVALID_BOUND")
    if (
        not isinstance(media_type, str)
        or not 1 <= len(media_type) <= 128
        or any(ord(character) < 32 or ord(character) > 126 for character in media_type)
    ):
        raise MobilitySourceError("INVALID_SPEC")
    return {"sha256": digest, "size_bytes": size, "media_type": media_type}


class ArtifactMobilitySourceService:
    """Application service that can act only through a binding-issued authority."""

    def __init__(self, store: SourceStore, *, authorizer) -> None:
        self._store = store
        self._authorizer = authorizer

    def _authority(
        self, context: object, action: str, trusted_provenance: object = None
    ) -> SourceAuthority:
        if action not in ("publish", "read") or not callable(self._authorizer):
            raise MobilitySourceError("UNAVAILABLE")
        try:
            authority = self._authorizer(context, action, trusted_provenance)
        except PermissionError:
            raise MobilitySourceError("FORBIDDEN") from None
        except MobilitySourceError:
            raise
        except Exception:
            raise MobilitySourceError("UNAVAILABLE") from None
        if not isinstance(authority, SourceAuthority):
            raise MobilitySourceError("FORBIDDEN")
        return authority

    def publish_sealed(
        self, stream, immutable_spec: object, trusted_provenance: object, context: object
    ) -> dict:
        authority = self._authority(context, "publish", trusted_provenance)
        if not callable(getattr(stream, "read", None)):
            raise MobilitySourceError("INVALID_STREAM")
        return self._store.publish_sealed(
            stream, _immutable_spec(immutable_spec, authority.limits), authority
        )

    def inspect_sealed(self, source_artifact_id: object, context: object) -> dict:
        authority = self._authority(context, "read")
        return self._store.inspect_sealed(_source_id(source_artifact_id), authority)

    def read_range(
        self, source_artifact_id: object, offset: object, length: object, context: object
    ) -> SourceArtifactRange:
        authority = self._authority(context, "read")
        if type(offset) is not int or not 0 <= offset <= authority.limits.max_object_bytes:
            raise MobilitySourceError("INVALID_BOUND")
        if (
            type(length) is not int
            or not 1 <= length <= authority.limits.max_range_bytes
        ):
            raise MobilitySourceError("INVALID_BOUND")
        return self._store.read_range(
            _source_id(source_artifact_id), offset, length, authority
        )

    def close(self) -> None:
        self._store.close()
