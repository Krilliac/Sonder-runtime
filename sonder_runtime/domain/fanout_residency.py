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
    planning.  Its original residency snapshot therefore cannot authorize a
    later model load.  Recheck immediately before the provider closure exists;
    a missing or unavailable row is a skipped receipt, never a fallback load.

    ``fetch_resident()`` returns the live Sonder Inference ``/v1/sonder/health``
    residency payload (``models: [{"id": ..., "state": "resident"}, ...]``);
    it is injected so the policy stays free of transport I/O.
    """
    if limits.get("selection_profile") != "loaded-local-chat":
        return ""
    try:
        payload = fetch_resident()
        rows = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("invalid Sonder Inference health residency response")
        resident: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            state = row.get("state")
            if state is not None and str(state).strip().lower() != "resident":
                continue
            name = str(row.get("id") or row.get("name") or row.get("model") or "").strip()
            if name:
                resident.add(name.casefold())
    except Exception:
        return "could not verify model residency at dispatch"
    if str(model or "").strip().casefold() not in resident:
        return "model is no longer resident at dispatch"
    return ""
