"""Ports the output-digest service reads through.

Adapters own the filesystem and the job registry; the service sees only a
bounded ``TextWindow`` and the job's string metadata.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TextWindow:
    """A bounded, already-guarded slice of one text source."""

    text: str
    label: str
    bytes_read: int
    source_bytes: int
    truncated: bool


class TextWindowSource(Protocol):
    """Read one guarded text file window (allowed roots, no-follow, bounded)."""

    def read_file_window(
        self,
        path: str,
        *,
        extra_roots: str,
        max_scan_bytes: int,
        tail_lines: int,
        timeout_seconds: float,
    ) -> TextWindow: ...


class JobOutputReader(Protocol):
    """Bounded access to one durable job's retained output."""

    def job_metadata(self, job_id: str) -> Mapping[str, str] | None:
        """The job's string metadata including ``kind``, or None when absent."""
        ...

    def read_output(
        self, job_id: str, *, max_bytes: int = 2_000_000, head_bytes: int = 65_536,
    ) -> TextWindow: ...


__all__ = ["JobOutputReader", "TextWindow", "TextWindowSource"]
