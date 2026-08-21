"""Compaction application services and the legacy compaction engine exports."""

from .append_service import (
    CompactionAppendError,
    CompactionAppendService,
    ImmutableSourceEventRange,
    StructuredCompaction,
)
from .session_service import SessionCompactionError, SessionCompactionService
from . import legacy as _legacy
from .legacy import *  # type: ignore[no-redef]

__all__ = [
    "CompactionAppendError", "CompactionAppendService",
    "ImmutableSourceEventRange", "StructuredCompaction",
    "SessionCompactionError", "SessionCompactionService",
    *_legacy.__all__,
]
