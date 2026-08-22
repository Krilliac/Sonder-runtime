"""Small, dependency-free safety boundary for paths and archive metadata.

The module validates names before an archive is extracted.  Callers must still
extract through a trusted implementation and re-check the destination while
writing; this module deliberately does not perform filesystem mutation.
"""

from __future__ import annotations

import os
import posixpath
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class UnsafePathError(ValueError):
    """Raised when a path cannot be proven safe for the requested operation."""


class ArchiveLimitError(ValueError):
    """Raised when archive metadata exceeds an explicit expansion limit."""


@dataclass(frozen=True)
class ProvenanceLabel:
    source: str
    trust: str = "untrusted"
    content_digest: str | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("provenance source is required")
        if self.trust not in {"untrusted", "reviewed", "trusted"}:
            raise ValueError("unsupported provenance trust label")


@dataclass(frozen=True)
class ArchiveLimits:
    max_entries: int = 10_000
    max_total_bytes: int = 1 << 30
    max_entry_bytes: int = 256 << 20
    max_path_length: int = 4_096

    def __post_init__(self) -> None:
        if min(self.max_entries, self.max_total_bytes, self.max_entry_bytes, self.max_path_length) < 1:
            raise ValueError("archive limits must be positive")


def _normalized_member_name(name: str, limit: int) -> str:
    if not isinstance(name, str) or not name or len(name) > limit:
        raise UnsafePathError("archive member name is empty or too long")
    # Archives use POSIX separators even on Windows.  Reject alternate drive
    # and UNC spellings before normalizing so they cannot become local paths.
    candidate = name.replace("\\", "/")
    if candidate.startswith("/") or candidate.startswith("//") or len(candidate) >= 2 and candidate[1] == ":":
        raise UnsafePathError("absolute archive member path")
    parts = candidate.split("/")
    if any(part in {"", "."} for part in parts):
        parts = [part for part in parts if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise UnsafePathError("archive path traversal")
    normalized = posixpath.normpath("/".join(parts))
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise UnsafePathError("archive path traversal")
    return normalized


def authorized_path(path: str | os.PathLike[str], roots: Iterable[str | os.PathLike[str]]) -> Path:
    """Return a resolved path only when it stays under an authorized root.

    Resolution is performed twice and every existing component is checked for
    symlinks.  This closes common check/use races for metadata validation; the
    eventual writer should use an equivalent no-follow/dir-handle operation.
    """
    raw = Path(path)
    if not raw.is_absolute():
        raise UnsafePathError("authorized paths must be absolute")
    root_paths = [Path(root).resolve(strict=True) for root in roots]
    if not root_paths:
        raise UnsafePathError("at least one authorized root is required")
    # Inspect the spelling supplied by the caller, not the resolved spelling;
    # otherwise a symlink can disappear during resolution.  Missing leaf
    # components are fine, but every existing prefix must be non-link.
    current = Path(raw.anchor)
    for component in raw.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                raise UnsafePathError("symlink path component")
        except OSError as exc:
            raise UnsafePathError("path could not be inspected") from exc
    for root in root_paths:
        try:
            candidate = raw.resolve(strict=False)
            candidate.relative_to(root)
            second = raw.resolve(strict=False)
            second.relative_to(root)
            if candidate != second:
                raise UnsafePathError("path changed during validation")
            return candidate
        except (OSError, RuntimeError, ValueError):
            continue
    raise UnsafePathError("path is outside authorized roots")


def validate_archive_members(
    members: Iterable[tuple[str, int, bool]],
    limits: ArchiveLimits = ArchiveLimits(),
) -> tuple[str, ...]:
    """Validate ``(name, size, is_link)`` metadata without opening members."""
    names: list[str] = []
    total = 0
    for index, (name, size, is_link) in enumerate(members, 1):
        if index > limits.max_entries:
            raise ArchiveLimitError("archive entry limit exceeded")
        if is_link:
            raise UnsafePathError("archive links are not permitted")
        if not isinstance(size, int) or size < 0 or size > limits.max_entry_bytes:
            raise ArchiveLimitError("archive member expansion limit exceeded")
        normalized = _normalized_member_name(name, limits.max_path_length)
        total += size
        if total > limits.max_total_bytes:
            raise ArchiveLimitError("archive total expansion limit exceeded")
        names.append(normalized)
    return tuple(names)


def inspect_zip(path: str | os.PathLike[str], limits: ArchiveLimits = ArchiveLimits()) -> tuple[str, ...]:
    with zipfile.ZipFile(path) as archive:
        return validate_archive_members(
            ((item.filename, item.file_size, bool((item.external_attr >> 16) & 0o170000 == 0o120000)) for item in archive.infolist()),
            limits,
        )


def inspect_tar(path: str | os.PathLike[str], limits: ArchiveLimits = ArchiveLimits()) -> tuple[str, ...]:
    with tarfile.open(path, "r:*") as archive:
        return validate_archive_members(
            ((item.name, item.size, item.issym() or item.islnk()) for item in archive.getmembers()),
            limits,
        )
