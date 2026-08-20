"""Pure formatting helpers for campaign run summaries."""

from __future__ import annotations


def campaign_headline(
    passed, total, recorded, failed_recorded, pitfall_errors, elapsed,
) -> str:
    """Build the durable first line for an unattended campaign run."""
    headline = (
        "campaign generate/compile/execute/record: "
        "%d/%d passed, %d recorded, %d failed-recorded"
        % (passed, total, recorded, failed_recorded)
    )
    if pitfall_errors:
        headline += ", %d pitfall-errors" % pitfall_errors
    return "%s in %.3fs" % (headline, elapsed)
