"""Bounded startup reconciliation for durable runtime state (WP9 OPS-004/005).

This module is deliberately persistence- and process-neutral.  Adapters read
session, job, child-agent, outbox, and activation records and pass immutable
observations here.  The result is a bounded set of classifications and
cleanup intents; a supervisor may later execute those intents under its own
permissions and platform-specific process-tree implementation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class RecordKind(str, Enum):
    SESSION = "session"
    JOB = "job"
    SUBAGENT = "subagent"
    OUTBOX = "outbox"
    ACTIVATION = "activation"


class ReconciliationClass(str, Enum):
    HEALTHY = "healthy"
    INTERRUPTED = "interrupted"
    ORPHANED = "orphaned"
    RESUMABLE = "resumable"
    TERMINAL = "terminal"


class DrainAction(str, Enum):
    KEEP = "keep"
    RESUME = "resume"
    MARK_INTERRUPTED = "mark_interrupted"
    DELIVER = "deliver"
    CLEANUP_PROCESS_TREE = "cleanup_process_tree"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class StartupObservation:
    """Read-only state captured by an adapter before reconciliation."""

    kind: RecordKind
    record_id: str
    status: str
    owner_instance_id: str = ""
    owner_alive: bool | None = None
    checkpoint_available: bool = False
    retryable: bool = False
    published: bool = True
    process_id: int | None = None
    process_group_id: int | None = None

    def __post_init__(self) -> None:
        if not self.record_id.strip() or not self.status.strip():
            raise ValueError("record_id and status are required")
        if self.process_id is not None and self.process_id <= 0:
            raise ValueError("process_id must be positive")
        if self.process_group_id is not None and self.process_group_id <= 0:
            raise ValueError("process_group_id must be positive")


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    observation: StartupObservation
    classification: ReconciliationClass
    resumable: bool
    action: DrainAction
    reason: str


@dataclass(frozen=True, slots=True)
class ProcessTreeCleanupIntent:
    """Bounded, non-executing intent for a later platform supervisor."""

    record_id: str
    process_id: int
    process_group_id: int | None
    max_descendants: int
    reason: str


@dataclass(frozen=True, slots=True)
class DrainPlan:
    accept_new_work: bool
    allow_new_claims: bool
    results: tuple[ReconciliationResult, ...]
    cleanup_intents: tuple[ProcessTreeCleanupIntent, ...]
    truncated: bool = False


TERMINAL = {
    RecordKind.JOB: {"succeeded", "failed", "cancelled"},
    RecordKind.SUBAGENT: {"succeeded", "failed", "cancelled", "timed_out"},
    RecordKind.SESSION: {"closed", "completed", "cancelled", "failed"},
    RecordKind.OUTBOX: {"published", "delivered"},
    RecordKind.ACTIVATION: {"activated", "rolled_back", "failed"},
}
ACTIVE = {"active", "claimed", "running", "planning", "downloading", "verifying", "draining", "activating"}
INTERRUPTED = {"interrupted", "timed_out"}
PENDING = {"pending", "queued", "created", "ready", "paused", "blocked", "unpublished", "available", "downloaded", "verified"}


def _classify(item: StartupObservation) -> tuple[ReconciliationClass, bool, DrainAction, str]:
    status = item.status.lower()
    if status in TERMINAL[item.kind]:
        return ReconciliationClass.TERMINAL, False, DrainAction.SKIP, "terminal record"
    if status in INTERRUPTED:
        can_resume = item.retryable or item.checkpoint_available or item.kind in {RecordKind.JOB, RecordKind.SUBAGENT}
        return (ReconciliationClass.RESUMABLE if can_resume else ReconciliationClass.INTERRUPTED,
                can_resume, DrainAction.RESUME if can_resume else DrainAction.MARK_INTERRUPTED,
                "durable interruption requires explicit recovery")
    if status in ACTIVE:
        if item.owner_alive is False:
            action = DrainAction.CLEANUP_PROCESS_TREE if item.process_id else DrainAction.MARK_INTERRUPTED
            return ReconciliationClass.ORPHANED, True, action, "active record owner is no longer alive"
        if not item.owner_instance_id:
            return ReconciliationClass.ORPHANED, True, DrainAction.MARK_INTERRUPTED, "active record has no owner"
        return ReconciliationClass.HEALTHY, False, DrainAction.KEEP, "active record has a live owner"
    if item.kind is RecordKind.OUTBOX and not item.published:
        return ReconciliationClass.RESUMABLE, True, DrainAction.DELIVER, "unpublished outbox event needs bounded delivery"
    if status in PENDING:
        return ReconciliationClass.RESUMABLE, True, DrainAction.RESUME, "pending record may be resumed by its owning service"
    return ReconciliationClass.INTERRUPTED, bool(item.retryable), DrainAction.MARK_INTERRUPTED, "unknown non-terminal state requires review"


def reconcile_observation(item: StartupObservation) -> ReconciliationResult:
    """Classify one durable observation without reading clocks or the OS."""
    classification, resumable, action, reason = _classify(item)
    return ReconciliationResult(item, classification, resumable, action, reason)


def build_drain_plan(
    observations: Iterable[StartupObservation],
    *,
    max_records: int = 100,
    max_process_descendants: int = 64,
) -> DrainPlan:
    """Create a bounded graceful-drain plan; never performs cleanup itself."""
    if isinstance(max_records, bool) or max_records <= 0:
        raise ValueError("max_records must be positive")
    if isinstance(max_process_descendants, bool) or max_process_descendants <= 0:
        raise ValueError("max_process_descendants must be positive")
    all_observations = list(observations)
    selected = all_observations[:max_records]
    results = tuple(reconcile_observation(item) for item in selected)
    intents = tuple(
        ProcessTreeCleanupIntent(
            result.observation.record_id,
            result.observation.process_id,
            result.observation.process_group_id,
            max_process_descendants,
            result.reason,
        )
        for result in results
        if result.action is DrainAction.CLEANUP_PROCESS_TREE
        and result.observation.process_id is not None
    )
    return DrainPlan(False, False, results, intents, truncated=len(all_observations) > max_records)


__all__ = [
    "DrainAction", "DrainPlan", "ProcessTreeCleanupIntent", "RecordKind",
    "ReconciliationClass", "ReconciliationResult", "StartupObservation",
    "build_drain_plan", "reconcile_observation",
]
