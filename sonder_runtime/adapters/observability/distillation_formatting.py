"""Operator-facing rendering for deferred lesson-distillation drains."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import TypedDict

    class DrainSummary(TypedDict, total=False):
        drained: int
        stored: int
        deferred: int
        failed: int
        skipped: int
        backlog: int | None


def _drain_backlog_text(drain: DrainSummary) -> str:
    """Render the drain's remaining backlog, or say it could not be read."""
    backlog = drain.get("backlog")
    return "unknown (count query failed)" if backlog is None else str(backlog)


def _drain_summary_text(drain: DrainSummary) -> str:
    """Render the campaign's one-line drain summary."""
    text = (
        "deferred distillations drained: %d (lessons stored %d, still "
        "deferred in batch %d, backlog remaining %s)"
        % (
            drain.get("drained", 0), drain.get("stored", 0),
            drain.get("deferred", 0), _drain_backlog_text(drain),
        )
    )
    if drain.get("failed"):
        text += " -- failed %d (recorder raised; these are NOT deferred and " \
                "will be retried)" % drain["failed"]
    return text
