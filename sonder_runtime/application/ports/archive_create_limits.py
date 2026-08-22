"""Typed, provider-neutral limits for bounded archive creation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ArchiveCreateLimits:
    """Effective archive limits after applying defaults and hard ceilings."""

    max_files: int = 2_000
    max_entries: int = 5_000
    max_file_bytes: int = 64_000_000
    max_total_bytes: int = 256_000_000
    max_depth: int = 32
    max_results: int = 2_500

    DEFAULT_MAX_FILES: ClassVar[int] = 2_000
    DEFAULT_MAX_ENTRIES: ClassVar[int] = 5_000
    DEFAULT_MAX_FILE_BYTES: ClassVar[int] = 64_000_000
    DEFAULT_MAX_TOTAL_BYTES: ClassVar[int] = 256_000_000
    DEFAULT_MAX_DEPTH: ClassVar[int] = 32
    DEFAULT_MAX_RESULTS: ClassVar[int] = 2_500

    HARD_MAX_FILES: ClassVar[int] = 10_000
    HARD_MAX_ENTRIES: ClassVar[int] = 20_000
    HARD_MAX_FILE_BYTES: ClassVar[int] = 256_000_000
    HARD_MAX_TOTAL_BYTES: ClassVar[int] = 1_000_000_000
    HARD_MAX_DEPTH: ClassVar[int] = 64
    HARD_MAX_RESULTS: ClassVar[int] = 10_000

    @classmethod
    def from_values(
        cls, max_files=None, max_entries=None, max_file_bytes=None,
        max_total_bytes=None, max_depth=None, max_results=None,
    ) -> "ArchiveCreateLimits":
        return cls(
            max_files=cls._bounded(max_files, cls.DEFAULT_MAX_FILES, cls.HARD_MAX_FILES),
            max_entries=cls._bounded(max_entries, cls.DEFAULT_MAX_ENTRIES, cls.HARD_MAX_ENTRIES),
            max_file_bytes=cls._bounded(
                max_file_bytes, cls.DEFAULT_MAX_FILE_BYTES, cls.HARD_MAX_FILE_BYTES,
            ),
            max_total_bytes=cls._bounded(
                max_total_bytes, cls.DEFAULT_MAX_TOTAL_BYTES, cls.HARD_MAX_TOTAL_BYTES,
            ),
            max_depth=cls._bounded(max_depth, cls.DEFAULT_MAX_DEPTH, cls.HARD_MAX_DEPTH),
            max_results=cls._bounded(max_results, cls.DEFAULT_MAX_RESULTS, cls.HARD_MAX_RESULTS),
        )

    @staticmethod
    def _bounded(value, default: int, ceiling: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        return max(1, min(ceiling, parsed))

    def as_dict(self) -> dict[str, int]:
        return {
            "max_files": self.max_files,
            "max_entries": self.max_entries,
            "max_file_bytes": self.max_file_bytes,
            "max_total_bytes": self.max_total_bytes,
            "max_depth": self.max_depth,
            "max_results": self.max_results,
        }


__all__ = ["ArchiveCreateLimits"]
