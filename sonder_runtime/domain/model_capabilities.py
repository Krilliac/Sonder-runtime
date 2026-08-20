"""Pure normalization of model-catalog capability metadata."""

from __future__ import annotations


def fanout_capabilities(record) -> set[str]:
    """Return normalized capabilities from a catalog record.

    Ollama-compatible catalogs may expose a scalar or collection at the top
    level, or place the same metadata under ``details``.  A non-empty
    top-level declaration is authoritative; otherwise the nested declaration
    is used.  The function is deliberately limited to explicit data so model
    routing callers can share one deterministic normalization rule.
    """
    record = record if isinstance(record, dict) else {}
    details = record.get("details") if isinstance(record.get("details"), dict) else {}

    def normalized(raw):
        if isinstance(raw, str):
            values = (raw,)
        elif isinstance(raw, (list, tuple, set)):
            values = raw
        else:
            return set()
        return {str(value).strip().casefold() for value in values if str(value).strip()}

    capabilities = normalized(record.get("capabilities"))
    return capabilities or normalized(details.get("capabilities"))
