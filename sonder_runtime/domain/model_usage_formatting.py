"""Pure formatting policies for persisted model-usage provenance."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Literal


def usage_source(
    tokens_in: int | None,
    tokens_out: int | None,
) -> Literal["ollama", "estimated", "mixed"]:
    """Classify the provenance of the two persisted provider token counts."""
    if tokens_in is not None and tokens_out is not None:
        return "ollama"
    if tokens_in is None and tokens_out is None:
        return "estimated"
    return "mixed"
