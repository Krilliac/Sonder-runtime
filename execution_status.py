"""Shared status contract for live fleet execution counts.

The fleet ledger is authoritative for these values. A "lane" is deliberately
narrow: one running fleet agent currently inside a model call. Direct chat,
autopilot, and activity spans are separate surfaces and are not added here,
which avoids double-counting nested work.
"""

from __future__ import annotations


def unavailable(error: str = "") -> dict:
    return {
        "known": False,
        "running_lanes": None,
        "running_agents": None,
        "queued_agents": None,
        "active_agents": None,
        "semantics": "fleet model-call lanes and durable fleet agents",
        "error": str(error or "")[:240],
    }


def from_fleet_snapshot(snapshot: dict | None) -> dict:
    if not isinstance(snapshot, dict):
        return unavailable("fleet snapshot is unavailable")
    try:
        active = int(snapshot["active_agents"])
        running = int(snapshot["running_agents"])
        queued = int(snapshot["queued_agents"])
        lanes = int(snapshot["active_model_calls"])
    except (KeyError, TypeError, ValueError) as exc:
        return unavailable("fleet snapshot is incomplete: %s" % exc)
    if (
        min(active, running, queued, lanes) < 0
        or running + queued != active
        or lanes > running
    ):
        return unavailable("fleet snapshot counts are inconsistent")
    return {
        "known": True,
        "running_lanes": lanes,
        "running_agents": running,
        "queued_agents": queued,
        "active_agents": active,
        "semantics": "fleet model-call lanes and durable fleet agents",
        "error": "",
    }


def with_feed(fleet_snapshot: dict | None, feed: dict | None) -> dict:
    """Compose fleet counts and the independently truthful activity feed."""
    result = (
        dict(fleet_snapshot)
        if isinstance(fleet_snapshot, dict) and fleet_snapshot.get("known") is False
        else from_fleet_snapshot(fleet_snapshot)
    )
    if isinstance(feed, dict):
        result["feed"] = feed
    else:
        result["feed"] = {
            "known": False,
            "active_responses": None,
            "events": [],
            "truncated": False,
            "redaction_applied": False,
            "schema_version": 1,
            "runtime_id": "",
            "oldest_seq": None,
            "next_seq": None,
            "dropped_events": None,
            "sequence_gap": None,
            "bytes": 0,
            "limits": {"events": 20, "preview_chars": 1000},
            "error": "activity feed is unavailable",
        }
    return result
