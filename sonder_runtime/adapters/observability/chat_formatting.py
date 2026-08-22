"""Presentation helpers for OpenAI-compatible chat responses."""


def chat_usage(response=None):
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
