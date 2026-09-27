"""Pure policies for normalizing provider-reported model usage."""

from __future__ import annotations


def usage_count(value):
    """Return a non-negative integer usage count, or ``None`` if invalid."""
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def merge_reasoning_response_usage(first, later, *, segments: int) -> dict:
    """Combine provider counters while retaining only the final response body."""
    merged = dict(later if isinstance(later, dict) else {})
    for key in (
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    ):
        left = first.get(key) if isinstance(first, dict) else None
        right = later.get(key) if isinstance(later, dict) else None
        if all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in (left, right)
        ):
            merged[key] = left + right
        elif isinstance(left, int) and not isinstance(left, bool) and left >= 0:
            merged[key] = left
    first_message = first.get("message") if isinstance(first, dict) else None
    first_thinking = (
        first_message.get("thinking") if isinstance(first_message, dict) else None
    )
    first_thinking_chars = (
        len(first_thinking) if isinstance(first_thinking, str) else 0
    )
    later_thinking_chars = None
    if isinstance(later, dict) and "reasoning_segments" in later:
        later_thinking_chars = usage_count(later.get("thinking_chars"))
    if later_thinking_chars is None:
        later_message = later.get("message") if isinstance(later, dict) else None
        later_thinking = (
            later_message.get("thinking")
            if isinstance(later_message, dict) else None
        )
        later_thinking_chars = (
            len(later_thinking) if isinstance(later_thinking, str) else 0
        )
    thinking_chars = first_thinking_chars + later_thinking_chars
    if thinking_chars > 0:
        merged["thinking_chars"] = thinking_chars
    merged["reasoning_segments"] = max(1, int(segments))
    return merged
