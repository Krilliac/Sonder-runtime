"""Application boundary for explainable context-item selection."""
from __future__ import annotations

from typing import TYPE_CHECKING

from sonder_runtime.domain.context.priority import (
    ContextItem,
    Selection,
    SelectionExplanation,
    eviction_order,
    select,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "ContextItem",
    "Selection",
    "SelectionExplanation",
    "eviction_order",
    "select_context",
]


def select_context(items: Iterable[ContextItem], *, budget: int) -> Selection:
    """Select context candidates and preserve a stable explanation manifest.

    [any thread, pure] The application layer owns this use-case boundary;
    producers remain responsible for creating candidates and adapters remain
    responsible for rendering selected content.
    """
    return select(tuple(items), budget)
