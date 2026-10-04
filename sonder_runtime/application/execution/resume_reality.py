"""Resume-time workspace reality and mutation authority.

The checkpoint carries a host observation of the workspace.  A resumed worker
may consume the bounded observation as request context, but it cannot regain
mutation authority until the trusted host records an inspection and, when the
changed scope requires it, a fresh plan.  This is a cooperative host-process
boundary; it is not an authentication boundary for arbitrary in-process code.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator, Mapping


RESUME_REALITY_HOST_KEY = "__sonder_host_resume_reality_v1"
RESUME_REALITY_CONTEXT_KEY = "resume_reality"
MAX_CONTEXT_BYTES = 16_384


def has_mutating_effects(effects) -> bool:
    """Read-only declarations remain usable for workspace reinspection."""
    return any(str(getattr(effect, "name", effect)).casefold() != "read_files" for effect in effects)


class ResumeMutationBlocked(PermissionError):
    """Raised when a resumed worker has not passed its reality barrier."""


def _bounded(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    candidate = {key: item for key, item in value.items() if key != "snapshot"}
    # The adapter already bounds its output.  This final guard prevents a
    # malformed port implementation from becoming an unbounded model prompt.
    import json

    encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return {
            "status": "delta_unavailable",
            "requires_reinspection": True,
            "requires_replan": True,
            "reason": "resume reality context exceeded bound",
        }
    return candidate


@dataclass
class ResumeBarrier:
    """Trusted host state for one resumed worker execution."""

    delta: Mapping[str, Any] | None = None
    inspected: bool = False
    replanned: bool = False

    def __post_init__(self) -> None:
        self.delta = _bounded(self.delta)
        self._context_consumed = False
        requires = bool(self.delta and self.delta.get("requires_reinspection"))
        needs_plan = bool(self.delta and self.delta.get("requires_replan"))
        self._blocked = requires or needs_plan
        self._needs_plan = needs_plan

    @property
    def blocked(self) -> bool:
        return self._blocked

    def record_inspection(self) -> None:
        """Record the host's completed inspection of the current workspace."""
        self.inspected = True
        if not self._needs_plan:
            self._blocked = False

    def record_replan(self, plan_digest: str | None = None) -> None:
        """Record a fresh host-approved plan after an intersecting change."""
        if not self.inspected:
            raise ResumeMutationBlocked("workspace inspection is required before replanning")
        if not isinstance(plan_digest, str) or not plan_digest.strip():
            raise ValueError("plan_digest must be non-empty text")
        self.replanned = True
        self._blocked = False

    def require_mutation_allowed(self) -> None:
        if self._blocked:
            reason = "workspace must be inspected and replanned before mutation" if self._needs_plan else "workspace must be inspected before mutation"
            raise ResumeMutationBlocked(reason)

    def consume_context(self) -> Mapping[str, Any] | None:
        """Return the resume delta once for the next request only."""
        if self._context_consumed:
            return None
        self._context_consumed = True
        return self.delta


_CURRENT: ContextVar[ResumeBarrier | None] = ContextVar("sonder_resume_barrier", default=None)


@contextmanager
def bound(barrier: ResumeBarrier | None) -> Iterator[ResumeBarrier | None]:
    token = _CURRENT.set(barrier)
    try:
        yield barrier
    finally:
        _CURRENT.reset(token)


bound_resume_barrier = bound


def current() -> ResumeBarrier | None:
    return _CURRENT.get()


def require_mutation_allowed() -> None:
    barrier = current()
    if barrier is not None:
        barrier.require_mutation_allowed()


def record_inspection() -> None:
    barrier = current()
    if barrier is None:
        raise ResumeMutationBlocked("no trusted resume barrier is bound")
    barrier.record_inspection()


def record_replan(plan_digest: str | None = None) -> None:
    barrier = current()
    if barrier is None:
        raise ResumeMutationBlocked("no trusted resume barrier is bound")
    barrier.record_replan(plan_digest)


def consume_resume_context() -> Mapping[str, Any] | None:
    barrier = current()
    return barrier.consume_context() if barrier is not None else None


__all__ = [
    "MAX_CONTEXT_BYTES", "RESUME_REALITY_CONTEXT_KEY", "RESUME_REALITY_HOST_KEY",
    "ResumeBarrier", "ResumeMutationBlocked", "bound", "bound_resume_barrier", "consume_resume_context",
    "current", "record_inspection", "record_replan", "require_mutation_allowed",
]
