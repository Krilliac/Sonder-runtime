"""Compatibility re-export of the compaction engine under its pre-package path."""
from __future__ import annotations

from .engine import (
    CompactionApplicationService,
    CompactionEngine,
    DeterministicCompactionEngine,
    canonical_summary,
)

__all__ = [
    "CompactionApplicationService", "DeterministicCompactionEngine", "CompactionEngine",
    "canonical_summary",
]
