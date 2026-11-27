"""Protocol adapter for Ollama model-inventory payloads."""

from __future__ import annotations

from sonder_runtime.adapters.model_transport import ModelCallError


def inventory_rows(payload: object, endpoint: str) -> list[dict[str, object]]:
    """Return valid inventory rows or raise for a malformed provider envelope.

    Accepts Sonder Inference OpenAI-list envelopes (``{"object": "list",
    "data": [...]}``) as well as legacy ``{"models": [...]}`` shapes, and
    normalizes each row so ``name`` and ``model`` are populated from ``id``.
    """
    if isinstance(payload, dict):
        rows = payload.get("data")
        if not isinstance(rows, list):
            rows = payload.get("models")
        if isinstance(rows, list):
            normalized: list[dict[str, object]] = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                entry = dict(row)
                model_id = str(
                    entry.get("name") or entry.get("model") or entry.get("id") or ""
                ).strip()
                if model_id:
                    entry.setdefault("name", model_id)
                    entry.setdefault("model", model_id)
                normalized.append(entry)
            return normalized
    raise ModelCallError("protocol", "invalid Sonder Inference %s response" % endpoint)


__all__ = ["inventory_rows"]
