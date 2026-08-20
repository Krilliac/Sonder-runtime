"""Pure formatting policies for composing prompt sections."""

from __future__ import annotations


def join_system_parts(*parts) -> str:
    """Join non-empty prompt sections with one blank line between sections."""
    return "\n\n".join(part for part in parts if part)
