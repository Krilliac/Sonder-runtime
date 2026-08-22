"""Pure formatting policies for persisted model-usage provenance."""

from __future__ import annotations


def usage_source(tokens_in, tokens_out):
    """Classify the provenance of the two persisted provider token counts."""
    if tokens_in is not None and tokens_out is not None:
        return "ollama"
    if tokens_in is None and tokens_out is None:
        return "estimated"
    return "mixed"
