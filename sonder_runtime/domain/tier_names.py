"""Pure formatting policy for presenting currently available model tiers."""
from __future__ import annotations

from collections.abc import Iterable, Mapping


def valid_tier_names(tiers: Mapping[str, object] | Iterable[str]) -> str:
    """Render tier keys using the legacy comma-separated presentation."""
    keys = tiers.keys() if isinstance(tiers, Mapping) else tiers
    return ", ".join(keys)


__all__ = ["valid_tier_names"]
