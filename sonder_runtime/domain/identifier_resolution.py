"""Normalize session and project identifier strings."""

from __future__ import annotations


def resolve_identifier(value, default):
    """Normalize an identifier: empty becomes *default*, ``"none"`` becomes None."""
    s = (value or "").strip()
    if s == "":
        return default
    if s.lower() == "none":
        return None
    return s
