"""Pure context-size formatting and estimation policies."""

from __future__ import annotations


def rough_token_count(text) -> int:
    """Return the cheap, dependency-free token estimate used by health views."""
    if not text:
        return 0
    return max(1, (len(str(text)) + 3) // 4)


__all__ = ["rough_token_count"]
