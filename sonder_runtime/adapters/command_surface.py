"""Compatibility adapter for legacy command/runtime hooks.

The root ``server`` module remains a legacy composition root during the
strangler migration.  Only this adapter knows how to translate its MCP hooks
to the typed application command surface; entrypoints and application code do
not import it directly.
"""
from __future__ import annotations


class LegacyServerMcpRuntime:
    """Adapt the legacy server's startup fence and MCP runner."""

    def require_startup_safety(self) -> None:
        import server

        server.require_mcp_startup_safety()

    def run(self, *, safety_checked: bool) -> None:
        import server

        server.run_mcp(safety_checked=safety_checked)


__all__ = ["LegacyServerMcpRuntime"]
