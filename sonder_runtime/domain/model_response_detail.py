"""Pure description of an empty model response.

When a model returns no assistant content, the caller needs enough shape
metadata to diagnose the failure without the private reasoning stream ever
reaching a log or receipt. Moved from ``server.py`` in the WP1
Three-Hundred-Fourteenth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json

from sonder_runtime.domain.model_usage import usage_count


def empty_model_response_detail(out, message):
    """Describe an empty response without exposing model reasoning content."""
    metadata = {}
    if isinstance(message, dict):
        thinking = message.get("thinking")
        if isinstance(thinking, str):
            metadata["thinking_chars"] = len(thinking)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, (list, tuple)):
            metadata["tool_call_count"] = len(tool_calls)

    eval_count = usage_count(out.get("eval_count"))
    if eval_count is not None:
        metadata["eval_count"] = eval_count

    done_reason = out.get("done_reason")
    if isinstance(done_reason, str) and done_reason.strip():
        normalized_reason = done_reason.strip().casefold()
        metadata["done_reason"] = (
            normalized_reason
            if normalized_reason in {"stop", "length"}
            else "other"
        )

    detail = "Ollama returned no assistant content"
    if metadata:
        detail += "; metadata=" + json.dumps(metadata, sort_keys=True)
    return detail
