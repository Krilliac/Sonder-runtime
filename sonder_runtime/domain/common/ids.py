"""Identifier helpers shared across domains."""
from __future__ import annotations

import uuid


def new_id(prefix: str) -> str:
    """Prefixed opaque identifier, e.g. ``new_id("run") -> "run_ab12..."``."""
    if not prefix or not prefix.isidentifier():
        raise ValueError(f"invalid id prefix {prefix!r}")
    return f"{prefix}_{uuid.uuid4().hex}"


def is_id(value: str, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix + "_")
        and len(value) == len(prefix) + 33
    )
