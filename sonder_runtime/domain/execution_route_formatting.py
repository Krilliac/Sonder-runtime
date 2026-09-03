"""Format a human-readable execution-decision summary header."""

from __future__ import annotations


_MODE_LABELS = {
    "workbench": "foreground workbench",
    "autopilot": "persistent Autopilot",
    "fleet": "hardware-bounded fleet",
    "deferred": "Autopilot deferred",
}


def execution_route_header(
    mode: str,
    source: str,
    reason: str,
    confidence=None,
    tier: str = "",
    *,
    tiers_map: dict | None = None,
    local_tiers=(),
) -> str:
    lines = [
        "sonder execution decision",
        "  mode: %s" % _MODE_LABELS.get(mode, mode),
        "  source: %s" % source,
        "  reason: %s" % reason,
    ]
    if tier in local_tiers:
        mapped = (tiers_map or {}).get(tier, "(unmapped)")
        lines.append("  tier: %s -> %s" % (tier, mapped))
    if confidence is not None:
        lines.append("  confidence: %.0f%%" % (float(confidence) * 100.0))
    lines.append(
        "  boundary: local tiers and existing host permissions, roots, and budgets"
    )
    return "\n".join(lines)
