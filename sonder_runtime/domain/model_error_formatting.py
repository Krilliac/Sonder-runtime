"""Pure policy for redacting and bounding model error details."""

from __future__ import annotations

import json
import re

_SECRET_KEY_PARTS = (
    "password", "passwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "cookie",
)
# Error bodies should not become a second persistence surface for bearer
# tokens or API keys accidentally echoed by an upstream proxy.
_SECRET_VALUE = re.compile(
    r"(?i)\b(bearer|token|secret|api[-_]?key)\b\s*[:=]?\s*"
    r"(?!(?:limit|count|budget|window|usage|quota|length|context|maximum|minimum)\b)"
    r"\S+"
)


def _scrub_text(text: str) -> str:
    return _SECRET_VALUE.sub(r"\1=<redacted>", text)


def redact_model_error_value(value, depth: int = 0):
    """Return a bounded, credential-safe representation of a model error.

    Values under a secret-named key are replaced, and every string leaf gets
    the same bearer/token/api-key scrub as an unstructured error, so a proxy
    message such as ``{"message": "Authorization: Bearer ..."}`` keeps no
    credential.
    """
    if depth > 4:
        return "<nested>"
    if isinstance(value, dict):
        redacted = {}
        for key, item in list(value.items())[:64]:
            name = str(key)
            lowered = name.lower().replace("-", "_")
            if any(part in lowered for part in _SECRET_KEY_PARTS):
                redacted[name] = "<redacted>"
            else:
                redacted[name] = redact_model_error_value(item, depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_model_error_value(item, depth + 1) for item in list(value)[:64]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    # Strings, and any non-JSON leaf (bytes, a set, an exception object) as
    # its text, so the leaf is scrubbed and json.dumps cannot raise on it.
    return _scrub_text(value if isinstance(value, str) else str(value))


def safe_model_error_detail(value, limit: int = 600) -> str:
    """Normalize an error detail while scrubbing credential-shaped values."""
    structured = isinstance(value, (dict, list, tuple))
    if structured:
        # Leaves are already scrubbed strings or JSON scalars. ``default=str``
        # is a last guard so error handling itself never raises here.
        value = json.dumps(
            redact_model_error_value(value), ensure_ascii=False, sort_keys=True, default=str,
        )
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not structured:
        text = _scrub_text(text)
    return text[:limit] or "model request failed"


def embedded_model_error(result) -> str:
    """Extract and safely format an in-band error from a model response."""
    if not isinstance(result, dict):
        return ""
    embedded = result.get("error")
    if not embedded:
        return ""
    return safe_model_error_detail(embedded)
