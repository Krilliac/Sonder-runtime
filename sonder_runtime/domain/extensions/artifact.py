"""Typed evidence for an extension artifact admitted to the registry."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ExtensionArtifactReceipt:
    """Immutable verification evidence bound to one local artifact."""

    path: str
    artifact_digest: str
    byte_count: int
    source: str = ""
    verified: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip() or len(self.path) > 4096:
            raise ValueError("artifact path must be bounded non-empty text")
        if not isinstance(self.artifact_digest, str) or not _SHA256.fullmatch(self.artifact_digest):
            raise ValueError("artifact_digest must be a lowercase SHA-256 digest")
        if isinstance(self.byte_count, bool) or not isinstance(self.byte_count, int) or self.byte_count <= 0:
            raise ValueError("artifact byte_count must be a positive integer")
        if not isinstance(self.source, str) or len(self.source) > 2048:
            raise ValueError("artifact source must be bounded text")
        if not isinstance(self.verified, bool):
            raise TypeError("artifact verified must be boolean")

    @classmethod
    def from_verification(cls, result: Mapping[str, object]) -> "ExtensionArtifactReceipt":
        """Convert the existing artifact-verification result without trusting labels."""
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise ValueError("artifact verification did not produce an accepted result")
        path, digest, byte_count = result.get("path"), result.get("sha256"), result.get("bytes")
        if not isinstance(path, str) or not isinstance(digest, str) or not isinstance(byte_count, int):
            raise ValueError("artifact verification omitted bounded identity fields")
        source = result.get("final_url") or result.get("url") or ""
        return cls(path, digest.lower(), byte_count, source if isinstance(source, str) else "")


__all__ = ["ExtensionArtifactReceipt"]
