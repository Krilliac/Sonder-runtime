"""Presentation helpers for OpenAI-compatible chat responses."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any


def chat_usage(response: Mapping[str, Any] | None = None) -> dict[str, int]:
    """Return bounded OpenAI usage fields from an activity span."""
    try:
        prompt = max(0, int((response or {}).get("tokens_in") or 0))
        completion = max(0, int((response or {}).get("tokens_out") or 0))
    except (AttributeError, TypeError, ValueError, OverflowError):
        prompt = completion = 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
