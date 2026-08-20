"""Pure output, timeout, and retention policy for the launcher."""
from __future__ import annotations


MAX_OPERATION_OUTPUT = 20_000
DEFAULT_OPERATION_RETENTION = 100
MAX_OPERATION_RETENTION = 1_000


def output_text(*values, limit=MAX_OPERATION_OUTPUT):
    """Join non-empty values and retain only the bounded output tail."""
    chunks = []
    for value in values:
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        value = str(value).strip()
        if value:
            chunks.append(value)
    output = "\n".join(chunks)
    if len(output) <= limit:
        return output
    marker = "[output truncated]\n"
    return marker + output[-(limit - len(marker)):]


def bounded_seconds(value, default, maximum):
    """Coerce a timeout into the launcher's inclusive safe range."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(1.0, min(parsed, float(maximum)))


def retention_limit(
    value,
    default=DEFAULT_OPERATION_RETENTION,
    maximum=MAX_OPERATION_RETENTION,
):
    """Coerce operation retention to a positive bounded integer."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


__all__ = [
    "DEFAULT_OPERATION_RETENTION",
    "MAX_OPERATION_OUTPUT",
    "MAX_OPERATION_RETENTION",
    "bounded_seconds",
    "output_text",
    "retention_limit",
]
