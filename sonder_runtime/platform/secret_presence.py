"""Pure policy for redacting whether a secret value is present."""

from __future__ import annotations


def redact_presence(value: str) -> str:
    """Return a stable, non-sensitive marker for secret presence."""
    return "[set]" if value else "[unset]"
