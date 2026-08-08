"""Identifier helpers shared across domains."""
from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Prefixed opaque identifier, e.g. ``new_id("run") -> "run_ab12..."``."""
    if not prefix or not prefix.isidentifier():
        raise ValueError(f"invalid id prefix {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex}"


def is_id(value: str, prefix: str) -> bool:
    if not isinstance(value, str) or not value.startswith(prefix + "_"):
        return False
    suffix = value[len(prefix) + 1:]
    # Length-only validation accepted arbitrary punctuation as an opaque ID,
    # allowing malformed selectors to cross domain boundaries as valid IDs.
    return len(suffix) == 32 and all(char in "0123456789abcdef" for char in suffix)
