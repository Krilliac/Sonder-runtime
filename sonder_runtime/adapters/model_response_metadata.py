"""Scalar-safe metadata parsed back out of an empty-response transport error.

The empty-response detail serializes a small allowlisted JSON object; this
parser accepts only that shape and treats every other error or detail as
opaque, so provider bodies and reasoning text never become durable
observations. It reads the transport's ``ModelCallError``, so it lives with
the adapters. Moved from ``server.py`` in the WP1 Three-Hundred-Nineteenth
Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import json

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.model_usage import usage_count


def response_error_metadata(error) -> dict:
    """Extract the scalar-safe metadata embedded in an empty-response error.

    ``_empty_model_response_detail`` deliberately serializes a small
    allowlisted JSON object.  This parser treats all other errors/details as
    opaque and returns no metadata, preventing provider bodies or reasoning
    text from becoming durable observations.
    """
    if not isinstance(error, ModelCallError) or error.kind != "empty_response":
        return {}
    prefix = "Ollama returned no assistant content; metadata="
    detail = str(error.detail or "")
    if not detail.startswith(prefix):
        return {}
    try:
        source = json.loads(detail[len(prefix):])
    except (TypeError, ValueError, RecursionError):
        return {}
    if not isinstance(source, dict):
        return {}
    metadata = {}
    thinking_chars = usage_count(source.get("thinking_chars"))
    if thinking_chars is not None and thinking_chars > 0:
        metadata["thinking_chars"] = thinking_chars
    done_reason = source.get("done_reason")
    if isinstance(done_reason, str):
        normalized = done_reason.strip().casefold()
        if normalized:
            metadata["done_reason"] = normalized if normalized in {"stop", "length"} else "other"
    return metadata
