"""Pure policy for redacting and bounding model error details."""

from __future__ import annotations

import json
import re


def redact_model_error_value(value, depth: int = 0):
    """Return a bounded, credential-safe representation of a model error."""
    if depth > 4:
        return "<nested>"
    if isinstance(value, dict):
        redacted = {}
        for key, item in list(value.items())[:64]:
            name = str(key)
            lowered = name.lower().replace("-", "_")
            if any(part in lowered for part in (
                "password", "passwd", "secret", "token", "api_key",
                "authorization", "credential",
            )):
                redacted[name] = "<redacted>"
            else:
                redacted[name] = redact_model_error_value(item, depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_model_error_value(item, depth + 1) for item in list(value)[:64]]
    return value


def safe_model_error_detail(value, limit: int = 600) -> str:
    """Normalize an error detail while scrubbing credential-shaped values."""
    structured = isinstance(value, (dict, list, tuple))
    if structured:
        value = json.dumps(
            redact_model_error_value(value), ensure_ascii=False, sort_keys=True,
        )
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    # Error bodies should not become a second persistence surface for bearer
    # tokens or API keys accidentally echoed by an upstream proxy.
    if not structured:
        text = re.sub(
            r"(?i)\b(bearer|token|secret|api[-_]?key)\b\s*[:=]?\s*"
            r"(?!(?:limit|count|budget|window|usage|quota|length|context|maximum|minimum)\b)"
            r"\S+",
            r"\1=<redacted>",
            text,
        )
    return text[:limit] or "model request failed"
