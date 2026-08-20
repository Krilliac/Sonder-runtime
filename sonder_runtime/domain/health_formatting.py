"""Pure formatting helpers for runtime health meters."""

from __future__ import annotations


def health_bar(percent, width=18) -> str:
    """Render a bounded fractional value as a fixed-width health meter."""
    pct = max(0.0, min(1.0, float(percent or 0.0)))
    filled = int(round(pct * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"
