"""Application-layer error re-exports (SPEC-5 §28).

Interfaces import errors from here, not from domain.common.errors.
"""
from __future__ import annotations

from ..domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    Conflict,
    ConcurrencyConflict,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    IntegrityFailure,
    InternalFailure,
    InvalidInput,
    MigrationRequired,
    NotFound,
    SonderError,
    Unauthenticated,
)

__all__ = [
    "Cancelled",
    "CapacityExceeded",
    "Conflict",
    "ConcurrencyConflict",
    "DeadlineExceeded",
    "DependencyUnavailable",
    "Forbidden",
    "IntegrityFailure",
    "InternalFailure",
    "InvalidInput",
    "MigrationRequired",
    "NotFound",
    "SonderError",
    "Unauthenticated",
]
