"""Application error taxonomy (SPEC-3 section 10).

Adapters map these to protocol-specific errors; stable codes stay
consistent across HTTP, MCP, and CLI. Ports raise these — never
``urllib``, ``sqlite3``, ``OSError``, or HTTP exceptions.
"""
from __future__ import annotations


class SonderError(Exception):
    """Base for all domain/application errors."""

    code = "INTERNAL_FAILURE"
    retryable = False


class InvalidInput(SonderError):
    code = "INVALID_INPUT"


class Unauthenticated(SonderError):
    code = "UNAUTHENTICATED"


class Forbidden(SonderError):
    code = "FORBIDDEN"


class NotFound(SonderError):
    code = "NOT_FOUND"


class Conflict(SonderError):
    code = "CONFLICT"


class ConcurrencyConflict(Conflict):
    code = "CONCURRENCY_CONFLICT"
    retryable = True


class CapacityExceeded(SonderError):
    code = "CAPACITY_EXCEEDED"
    retryable = True


class DependencyUnavailable(SonderError):
    code = "DEPENDENCY_UNAVAILABLE"
    retryable = True


class DeadlineExceeded(SonderError):
    code = "DEADLINE_EXCEEDED"
    retryable = True


class Cancelled(SonderError):
    code = "CANCELLED"


class IntegrityFailure(SonderError):
    code = "INTEGRITY_FAILURE"


class MigrationRequired(SonderError):
    code = "MIGRATION_REQUIRED"


class InternalFailure(SonderError):
    code = "INTERNAL_FAILURE"
