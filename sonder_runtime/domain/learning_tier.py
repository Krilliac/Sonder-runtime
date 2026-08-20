"""Pure policy for mapping recorded learning labels to canonical tiers."""

from __future__ import annotations


def canonical_learn_tier(tier_label):
    """Map a recorded tier label to the learning tier that governs it."""
    return "code" if tier_label == "sonder" else tier_label
