"""Pure context-size formatting and estimation policies."""

from __future__ import annotations


def rough_token_count(text) -> int:
    """Return the cheap, dependency-free token estimate used by health views."""
    if not text:
        return 0
    return max(1, (len(str(text)) + 3) // 4)


def rough_token_count_from_chars(count) -> int:
    """Return the same estimate when only a character count is available."""
    count = max(0, int(count or 0))
    return max(1, (count + 3) // 4) if count else 0


__all__ = ["rough_token_count", "rough_token_count_from_chars"]
