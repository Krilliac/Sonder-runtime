"""Protocol adapter for Ollama model-inventory payloads."""

from __future__ import annotations

from sonder_runtime.adapters.model_transport import ModelCallError


def inventory_rows(payload, endpoint):
    """Return valid inventory rows or raise for a malformed provider envelope.

    A provider response must be an object whose ``models`` member is a list.
    Individual malformed rows are skipped so a partially useful inventory is
    preserved, while a malformed envelope becomes an explicit protocol error
    instead of being mistaken for an empty catalog.
    """
    if isinstance(payload, dict):
        rows = payload.get("models")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    raise ModelCallError("protocol", "invalid Ollama %s response" % endpoint)


__all__ = ["inventory_rows"]
