"""Parse and clamp an approval listing limit from user input.
"""
from __future__ import annotations


def approvals_limit(arg: str) -> int:
    text = str(arg or "").strip()
    if not text:
        return 20
    try:
        return max(1, min(int(text), 200))
    except ValueError:
        return 20
