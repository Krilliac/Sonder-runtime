"""Deadline-aware placement scoring.

Extends placement decisions with deadline constraints.  Given a
workload's deadline and a node's estimated completion time (derived
from load, queue depth, and historical round-trip), the scorer
adjusts placement priority or rejects nodes that cannot meet the
deadline.

No I/O, no threading -- pure scoring functions.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeadlineConstraint:
    deadline_at: float
    reject_if_late: bool = True
    min_remaining_fraction: float = 0.2


@dataclass(frozen=True, slots=True)
class NodeEstimate:
    node_id: str
    estimated_completion_ms: float
    queue_depth: int
    load_fraction: float
    round_trip_ms: float


@dataclass(frozen=True, slots=True)
class DeadlineScoredNode:
    node_id: str
    base_score: float
    deadline_score: float
    final_score: float
    meets_deadline: bool
    estimated_completion_ms: float
    slack_ms: float


def estimate_completion(
    node: NodeEstimate,
    base_processing_ms: float,
) -> float:
    load_factor = 1.0 + node.load_fraction
    queue_factor = 1.0 + node.queue_depth * 0.5
    return (base_processing_ms * load_factor * queue_factor) + node.round_trip_ms


def score_with_deadline(
    node: NodeEstimate,
    base_score: float,
    constraint: DeadlineConstraint,
    base_processing_ms: float = 1000.0,
    *,
    now: float | None = None,
) -> DeadlineScoredNode:
    ts = now if now is not None else time.monotonic()
    remaining_ms = (constraint.deadline_at - ts) * 1000.0

    est = estimate_completion(node, base_processing_ms)
    slack = remaining_ms - est
    meets = slack >= 0

    if remaining_ms <= 0:
        return DeadlineScoredNode(
            node_id=node.node_id,
            base_score=base_score,
            deadline_score=-1000.0,
            final_score=base_score - 1000.0,
            meets_deadline=False,
            estimated_completion_ms=est,
            slack_ms=slack,
        )

    slack_ratio = slack / remaining_ms if remaining_ms > 0 else 0.0
    deadline_bonus = slack_ratio * 100.0 if meets else -100.0 * (1.0 - slack_ratio)

    return DeadlineScoredNode(
        node_id=node.node_id,
        base_score=base_score,
        deadline_score=round(deadline_bonus, 2),
        final_score=round(base_score + deadline_bonus, 2),
        meets_deadline=meets,
        estimated_completion_ms=round(est, 2),
        slack_ms=round(slack, 2),
    )


def filter_by_deadline(
    candidates: list[DeadlineScoredNode],
    constraint: DeadlineConstraint,
) -> list[DeadlineScoredNode]:
    if constraint.reject_if_late:
        viable = [c for c in candidates if c.meets_deadline]
    else:
        viable = list(candidates)

    viable.sort(key=lambda c: c.final_score, reverse=True)
    return viable


def has_budget_for_retry(
    constraint: DeadlineConstraint,
    *,
    now: float | None = None,
) -> bool:
    ts = now if now is not None else time.monotonic()
    total_budget = constraint.deadline_at - ts
    if total_budget <= 0:
        return False
    return True


def remaining_budget_fraction(
    constraint: DeadlineConstraint,
    started_at: float,
    *,
    now: float | None = None,
) -> float:
    ts = now if now is not None else time.monotonic()
    total = constraint.deadline_at - started_at
    if total <= 0:
        return 0.0
    elapsed = ts - started_at
    return max(0.0, min(1.0, 1.0 - elapsed / total))
