"""Pure policy for mapping recorded learning labels to canonical tiers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Container


def canonical_learn_tier(tier_label: str | None) -> str | None:
    """Map a recorded tier label to the learning tier that governs it."""
    return "code" if tier_label == "sonder" else tier_label


def should_learn(
    tier: str | None,
    learn: bool,
    learning_tiers: Container[str],
) -> bool:
    """Return whether a caller should feed this tier into learning.

    The caller supplies the already-resolved tier set because environment
    configuration belongs at the composition boundary, not in this pure
    policy module.
    """
    return bool(learn) and tier in learning_tiers
