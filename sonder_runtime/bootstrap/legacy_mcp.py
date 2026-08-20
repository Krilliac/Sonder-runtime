"""Legacy MCP composition boundary.

Only this bounded bootstrap module resolves the historical ``server`` root.
The application command and its adapter receive plain callable dependencies.
"""
from __future__ import annotations

from sonder_runtime.adapters.command_surface import LegacyServerMcpRuntime


def build_legacy_server_mcp_runtime() -> LegacyServerMcpRuntime:
    """Compose the legacy MCP hooks at the process boundary."""
    import server

    return LegacyServerMcpRuntime(
        require_startup_safety=server.require_mcp_startup_safety,
        run_mcp=server.run_mcp,
    )


__all__ = ["build_legacy_server_mcp_runtime"]
