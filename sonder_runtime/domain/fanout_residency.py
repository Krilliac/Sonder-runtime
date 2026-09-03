"""No-load fence for resident-only fanout targets at dispatch time.

A durable fanout may be resumed long after planning, so the original
residency snapshot cannot authorize a later model load; the fence rechecks
live residency immediately before the provider closure exists and skips a
target that is missing or unverifiable. The residency fetch is injected.
Moved from ``server.py`` in the WP1 Three-Hundred-Thirty-First Slice with
its behaviour byte-for-byte intact.
"""
from __future__ import annotations


def dispatch_residency_reason(limits, model, *, fetch_resident):
    """Return a no-load fence refusal for a selected resident-only target.

    A durable fanout may wait in the queue or be explicitly resumed long after
    planning.  Its original ``/api/ps`` snapshot therefore cannot authorize a
    later model load.  Recheck immediately before the provider closure exists;
    a missing or unavailable row is a skipped receipt, never a fallback load.

    ``fetch_resident()`` returns the live residency payload; it is injected so
    the policy stays free of the Ollama transport.
    """
    if limits.get("selection_profile") != "loaded-local-chat":
        return ""
    try:
        payload = fetch_resident()
        rows = payload.get("models", []) if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("invalid Ollama /api/ps response")
        resident = {
            str(row.get("name") or "").strip().casefold()
            for row in rows if isinstance(row, dict) and str(row.get("name") or "").strip()
        }
    except Exception:
        return "could not verify model residency at dispatch"
    if str(model or "").strip().casefold() not in resident:
        return "model is no longer resident at dispatch"
    return ""
