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


# Errors a fresh server.py raises when it imports a helper module that this
# process already holds at an older version (sys.modules is not refreshed for
# helpers outside the live-reload watch list). Observed 2026-10-08 after a
# 340-file fast-forward under a running MCP server: the new
# ``fanout_synthesis`` imported ``synthesis_rows`` from the cached, older
# ``fanout_receipt``. The refresh records only the exception class, so the
# same classes also cover an ordinary bad import, a missing dependency or a
# top-level NameError in server.py itself: skew is possible, never proven.
_SKEW_ERROR_TYPES = frozenset({"ImportError", "ModuleNotFoundError", "AttributeError", "NameError"})

_GENERIC_STALE_ACTION = "fix the source error or restart the Sonder MCP server to load server.py"


def stale_registry_action(data) -> str:
    """Operator action when the registry is stale behind a failed refresh.

    Returns an empty string unless the last refresh failed AND the source on
    disk differs from the loaded registry. The action always leads with the
    generic fix-or-restart: the recorded error is only an exception class,
    which cannot tell helper-version skew from a real source error, and a
    restart cannot fix a real source error (it may replace the working
    last-known-good registry with a process that cannot start). For
    import-shaped failures it adds, conditionally, the one case where only a
    restart helps.
    """
    error = str((data or {}).get("last_error") or "")
    loaded = str((data or {}).get("loaded_digest") or "")
    current = str((data or {}).get("current_digest") or "")
    if not error or not loaded or not current or loaded == current:
        return ""
    if error.partition(":")[0] in _SKEW_ERROR_TYPES:
        return (
            "%s; if server.py imports cleanly in a fresh interpreter, the cause is "
            "helper code this process holds at an older version, which live "
            "refresh does not reload, and only a restart helps" % _GENERIC_STALE_ACTION
        )
    return _GENERIC_STALE_ACTION


def capability_shadow_line(report: str, stale_action: str) -> str:
    """Render the diagnostics shadow line, qualified while the registry is stale.

    Watched helper modules (``tool_capabilities`` among them) live-reload even
    when ``server.py`` cannot, so a stale registry is compared against
    descriptors from newer source; the resulting mismatches are version skew,
    not descriptor drift, and must not read as plain drift.
    """
    prefix = (
        "UNRELIABLE (stale MCP registry; descriptors may be newer than the "
        "loaded tool surfaces) " if stale_action else ""
    )
    return "  tool capability shadow: %s%s" % (prefix, report)


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
        stale_action = stale_registry_action(data)
        if stale_action:
            lines.append("  ACTION: %s" % stale_action)
    if data.get("last_notification_error"):
        lines.append("  notification warning: MCP list-change notification failed")
    return "\n".join(lines)
