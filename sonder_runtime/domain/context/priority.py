"""Deterministic context-item priority and eviction policy.

This is a small policy boundary, not a context planner or compactor.  It only
answers which already-produced items fit a budget and records the reasons for
that answer.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextItem:
    """An immutable candidate supplied by a context producer."""

    item_id: str
    section: str
    cost: int
    priority: int
    source: str
    confidence: float | None = None
    protected: bool = False
    ordinal: int = 0

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id must be non-empty")
        if not self.section:
            raise ValueError("section must be non-empty")
        if not self.source:
            raise ValueError("source must be non-empty")
        if isinstance(self.cost, bool) or not isinstance(self.cost, int) or self.cost < 0:
            raise ValueError("cost must be a non-negative integer")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise ValueError("priority must be an integer")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class SelectionExplanation:
    item_id: str
    selected: bool
    reason: str
    cost: int
    source: str
    confidence: float | None
    priority: int
    protected: bool


@dataclass(frozen=True)
class Selection:
    selected: tuple[ContextItem, ...]
    omitted: tuple[ContextItem, ...]
    explanations: tuple[SelectionExplanation, ...]
    budget: int
    used: int
    emergency_overflow: bool


def _rank(item: ContextItem) -> tuple[int, int, int, str]:
    """Selection order: protection, priority, caller order, stable identity."""
    return (-int(item.protected), -item.priority, item.ordinal, item.item_id)


def _eviction_rank(item: ContextItem) -> tuple[int, int, int, str]:
    """Lowest-value items are evicted first, with stable tie breakers."""
    return (int(item.protected), item.priority, item.ordinal, item.item_id)


def select(items: tuple[ContextItem, ...] | list[ContextItem], budget: int) -> Selection:
    """Select candidates deterministically within ``budget``.

    Protected items are admitted first and are never evicted.  If their total
    cost exceeds the budget, all protected items remain selected and the result
    explicitly reports emergency overflow.  Optional items are then admitted
    in rank order when they fit; a larger item does not prevent later smaller
    items from being admitted.
    """
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        raise ValueError("budget must be a non-negative integer")
    candidates = tuple(items)
    if len({item.item_id for item in candidates}) != len(candidates):
        raise ValueError("item_id values must be unique")
    if any(not isinstance(item, ContextItem) for item in candidates):
        raise TypeError("items must contain ContextItem values")

    ordered = tuple(sorted(candidates, key=_rank))
    selected_ids: set[str] = set()
    used = 0
    for item in ordered:
        if item.protected:
            selected_ids.add(item.item_id)
            used += item.cost
    emergency = used > budget

    if not emergency:
        for item in ordered:
            if item.protected:
                continue
            if used + item.cost <= budget:
                selected_ids.add(item.item_id)
                used += item.cost

    selected = tuple(item for item in ordered if item.item_id in selected_ids)
    omitted = tuple(item for item in ordered if item.item_id not in selected_ids)
    reason_by_id = {}
    for item in omitted:
        reason_by_id[item.item_id] = (
            "emergency_overflow_protected_items_exceed_budget"
            if emergency
            else "omitted_budget"
        )
    explanations = tuple(
        SelectionExplanation(
            item_id=item.item_id,
            selected=item.item_id in selected_ids,
            reason="selected_protected" if item.protected and item.item_id in selected_ids
            else "selected_priority_fit" if item.item_id in selected_ids
            else reason_by_id[item.item_id],
            cost=item.cost,
            source=item.source,
            confidence=item.confidence,
            priority=item.priority,
            protected=item.protected,
        )
        for item in ordered
    )
    return Selection(selected, omitted, explanations, budget, used, emergency)


def eviction_order(items: tuple[ContextItem, ...] | list[ContextItem]) -> tuple[ContextItem, ...]:
    """Return the deterministic order in which non-selected items are evicted."""
    return tuple(sorted(items, key=_eviction_rank))
