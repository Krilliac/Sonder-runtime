"""Deterministic, bounded project/personal instruction discovery.

The registry reads only known filenames at explicitly supplied roots. It does
not recursively search arbitrary directories or follow symlinks, and later
sources replace an earlier record with the same logical name.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Iterable


MAX_INSTRUCTION_BYTES = 128 * 1024
MAX_INSTRUCTION_FILES = 32
_KNOWN_FILES = ("AGENTS.md", "ZERO.md", ".zero/AGENTS.md")
_DEFAULT_ORDER = ("bundled", "global", "project", "configured")


class InstructionDiscoveryError(ValueError):
    """Raised when instruction discovery input exceeds its safe contract."""


@dataclass(frozen=True, slots=True)
class InstructionSource:
    kind: str
    root: Path


@dataclass(frozen=True, slots=True)
class InstructionRecord:
    name: str
    source: str
    path: Path
    content: str
    sha256: str


class InstructionRegistry:
    """Discover known instruction files in low-to-high precedence order."""

    def __init__(
        self,
        sources: Iterable[InstructionSource] = (),
        *,
        max_bytes: int = MAX_INSTRUCTION_BYTES,
        max_files: int = MAX_INSTRUCTION_FILES,
    ) -> None:
        if not 1 <= max_bytes <= MAX_INSTRUCTION_BYTES:
            raise InstructionDiscoveryError("max_bytes is out of bounds")
        if not 1 <= max_files <= MAX_INSTRUCTION_FILES:
            raise InstructionDiscoveryError("max_files is out of bounds")
        self._sources = tuple(sources)
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._records: dict[str, InstructionRecord] = {}
        self.refresh()

    @classmethod
    def from_roots(cls, roots: dict[str, Path | str], **kwargs) -> "InstructionRegistry":
        sources = [
            InstructionSource(kind, Path(roots[kind]))
            for kind in _DEFAULT_ORDER
            if kind in roots
        ]
        sources.extend(
            InstructionSource(kind, Path(root))
            for kind, root in roots.items()
            if kind not in _DEFAULT_ORDER
        )
        return cls(sources, **kwargs)

    def refresh(self) -> None:
        records: dict[str, InstructionRecord] = {}
        scanned = 0
        for source in self._sources:
            root = source.root
            if not root.is_dir() or root.is_symlink():
                continue
            for relative in _KNOWN_FILES:
                path = root / relative
                if not path.is_file() or path.is_symlink():
                    continue
                scanned += 1
                if scanned > self._max_files:
                    raise InstructionDiscoveryError("instruction file count exceeds limit")
                try:
                    size = path.stat().st_size
                except OSError as exc:
                    raise InstructionDiscoveryError("cannot inspect instruction file") from exc
                if size > self._max_bytes:
                    raise InstructionDiscoveryError("instruction file exceeds byte limit")
                try:
                    content = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise InstructionDiscoveryError("cannot read instruction file") from exc
                if len(content.encode("utf-8")) > self._max_bytes:
                    raise InstructionDiscoveryError("instruction file exceeds byte limit")
                records[relative] = InstructionRecord(
                    relative, source.kind, path,
                    content, sha256(content.encode("utf-8")).hexdigest(),
                )
        self._records = records

    def records(self) -> tuple[InstructionRecord, ...]:
        return tuple(self._records[name] for name in sorted(self._records))

    def content(self) -> str:
        """Return selected instructions in deterministic precedence order."""
        return "\n\n".join(record.content for record in self.records())

    def __len__(self) -> int:
        return len(self._records)


__all__ = [
    "InstructionDiscoveryError", "InstructionRecord", "InstructionRegistry",
    "InstructionSource", "MAX_INSTRUCTION_BYTES", "MAX_INSTRUCTION_FILES",
]
