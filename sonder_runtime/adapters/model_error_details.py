"""Transport-facing extraction of bounded model error details."""

from __future__ import annotations

import json

from sonder_runtime.domain.model_error_formatting import safe_model_error_detail


def http_error_detail(error) -> str:
    """Read and safely format a bounded detail from an HTTP model error."""
    detail = getattr(error, "reason", "") or "HTTP %s" % error.code
    try:
        raw = error.read(4097)
        if raw:
            decoded = raw[:4096].decode("utf-8", errors="replace")
            try:
                parsed = json.loads(decoded)
                if isinstance(parsed, dict) and parsed.get("error"):
                    decoded = parsed["error"]
                elif isinstance(parsed, (dict, list)):
                    decoded = parsed
            except (TypeError, ValueError):
                pass
            detail = decoded
    except Exception:
        pass
    return safe_model_error_detail(detail)


def transport_error_detail(error) -> str:
    """Safely format the reason attached to a non-HTTP transport error."""
    reason = getattr(error, "reason", error)
    return safe_model_error_detail(reason)
