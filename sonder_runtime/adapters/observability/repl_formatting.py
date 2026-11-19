"""Pure presentation helpers used by the interactive REPL."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping


def elapsed_label(elapsed_ms: float | str | None) -> str:
    """Format a non-negative duration for terminal status chrome."""
    elapsed_ms = max(0, int(elapsed_ms or 0))
    if elapsed_ms < 1000:
        return "%dms" % elapsed_ms
    return "%.2fs" % (elapsed_ms / 1000.0)


def response_status_label(label: str | None = "Sonder", *, error: bool = False) -> str:
    """Return the short state label used by the interactive response panel."""
    state = "error" if error else "answer"
    return "%s · %s" % (str(label or "Sonder"), state)


def response_footer(
    box: Mapping[str, str],
    timing: str,
    *,
    offer_feedback: bool = False,
) -> str:
    """Build the stable footer for an interactive response panel."""
    feedback = "(/pass or /fail)" if offer_feedback else ""
    return "%s %s  %s" % (box["bl"], str(timing), feedback)
