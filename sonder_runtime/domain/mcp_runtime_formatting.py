"""Pure operator-facing rendering of the MCP runtime status.

The runtime data is collected by the reloadable MCP adapter; this module
only renders it and reduces a refresh failure to a safe, content-free error
line so a stack trace or path never reaches the operator surface. It is
explicit-input and side-effect free: the provenance recovery action is
injected by the caller. Moved from ``server.py`` in the WP1
Three-Hundred-Second Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import re


def safe_mcp_error(value) -> str:
    text = str(value or "")
    safe_messages = {
        "stale runtime source: loaded MCP file is unavailable",
        "configured runtime root is unavailable",
        "loaded MCP source does not match configured runtime root",
    }
    if text in safe_messages:
        return text
    error_type = text.partition(":")[0]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}(?:Error|Exception)", error_type):
        return "%s: source refresh failed" % error_type
    return "runtime source refresh failed"


def format_mcp_runtime(data, *, recovery_action) -> str:
    """Render the MCP runtime status block.

    ``recovery_action(provenance)`` returns the operator action for a
    provenance issue, or an empty string; the caller injects it so this
    renderer stays free of the reloadable-MCP adapter.
    """
    loaded = str(data.get("loaded_digest") or "")[:12] or "unknown"
    current = str(data.get("current_digest") or "")[:12] or "unknown"
    lines = [
        "sonder MCP runtime",
        "  status: %s | live source refresh: %s"
        % (
            data.get("status", "unknown"),
            "on" if data.get("enabled") else "off",
        ),
        "  tools: %s | atomic refreshes: %s | last surface changed: %s"
        % (
            data.get("registered_tools", 0),
            data.get("refresh_count", 0),
            "yes" if data.get("last_surface_changed") else "no",
        ),
        "  MCP tool-list updates: %s"
        % ("advertised" if data.get("protocol_list_changed") else "not advertised"),
        "  source registration: %s"
        % ("available" if data.get("path") else "unknown"),
        "  loaded/current: %s / %s" % (loaded, current),
    ]
    provenance = data.get("provenance") or {}
    if provenance:
        lines.extend([
            "  process: pid=%s | python=%s"
            % (
                provenance.get("pid", "unknown"),
                "python" if provenance.get("python") else "unknown",
            ),
            "  process cwd: %s"
            % (
                "unavailable"
                if provenance.get("cwd") == "(deleted or unavailable)"
                else "available"
            ),
            "  source root: %s"
            % ("present" if provenance.get("source_root_exists") else "missing"),
            "  configured runtime root: %s"
            % (
                "present"
                if provenance.get("configured_root_exists")
                else "missing/not set"
            ),
        ])
        if provenance.get("issue"):
            lines.append("  provenance ERROR: %s" % provenance["issue"])
        action = recovery_action(provenance)
        if action:
            lines.append("  ACTION: %s" % action)
    if data.get("last_refresh_ts"):
        lines.append("  last refresh unix time: %s" % data["last_refresh_ts"])
    if data.get("last_error"):
        lines.append(
            "  ERROR: %s (last known-good registry remains active)"
            % safe_mcp_error(data["last_error"])
        )
    if data.get("last_notification_error"):
        lines.append("  notification warning: MCP list-change notification failed")
    return "\n".join(lines)
