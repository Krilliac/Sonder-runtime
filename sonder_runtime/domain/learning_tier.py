"""Pure policy for mapping recorded learning labels to canonical tiers."""

from __future__ import annotations


def canonical_learn_tier(tier_label):
    """Map a recorded tier label to the learning tier that governs it."""
    return "code" if tier_label == "sonder" else tier_label


def should_learn(tier, learn, learning_tiers) -> bool:
    """Return whether a caller should feed this tier into learning.

    The caller supplies the already-resolved tier set because environment
    configuration belongs at the composition boundary, not in this pure
    policy module.
    """
    return bool(learn) and tier in learning_tiers
