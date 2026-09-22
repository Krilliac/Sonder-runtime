"""Scalar-safe metadata recovered from a model transport error.

Multi-segment callers can attach typed scalar usage directly to the error. An
empty-response detail can also serialize a small allowlisted JSON object. This
parser accepts only those fields and treats every other detail as opaque, so
provider bodies and reasoning text never become durable observations. It
reads the transport's ``ModelCallError``, so it lives with the adapters.
"""
from __future__ import annotations

import json

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.model_usage import usage_count


def response_error_metadata(error) -> dict:
    """Extract typed usage and allowlisted empty-response metadata.

    ``_empty_model_response_detail`` deliberately serializes a small
    allowlisted JSON object. Other error details remain opaque; only typed
    scalar usage can pass through, preventing provider bodies or reasoning
    text from becoming durable observations.
    """
    if not isinstance(error, ModelCallError):
        return {}
    metadata = {}
    thinking_chars = usage_count(getattr(error, "thinking_chars", None))
    if thinking_chars is not None and thinking_chars > 0:
        metadata["thinking_chars"] = thinking_chars
    reasoning_segments = usage_count(getattr(error, "reasoning_segments", None))
    if reasoning_segments is not None and reasoning_segments > 0:
        metadata["reasoning_segments"] = reasoning_segments
    if error.kind != "empty_response":
        return metadata
    prefix = "Ollama returned no assistant content; metadata="
    detail = str(error.detail or "")
    if not detail.startswith(prefix):
        return metadata
    try:
        source = json.loads(detail[len(prefix):])
    except (TypeError, ValueError, RecursionError):
        return metadata
    if not isinstance(source, dict):
        return metadata
    detail_thinking_chars = usage_count(source.get("thinking_chars"))
    if (
        "thinking_chars" not in metadata
        and detail_thinking_chars is not None
        and detail_thinking_chars > 0
    ):
        metadata["thinking_chars"] = detail_thinking_chars
    done_reason = source.get("done_reason")
    if isinstance(done_reason, str):
        normalized = done_reason.strip().casefold()
        if normalized:
            metadata["done_reason"] = normalized if normalized in {"stop", "length"} else "other"
    return metadata
