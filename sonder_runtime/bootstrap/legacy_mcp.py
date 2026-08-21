"""Legacy MCP composition boundary.

Only this bounded bootstrap module resolves the historical ``server`` root.
The application command and its adapter receive plain callable dependencies.
"""
from __future__ import annotations

from types import ModuleType

from sonder_runtime.adapters.command_surface import LegacyServerMcpRuntime
from .legacy_root import runtime as legacy_runtime


def build_legacy_server_mcp_runtime(
    runtime: ModuleType | None = None,
) -> LegacyServerMcpRuntime:
    """Compose the legacy MCP hooks at the process boundary."""
    runtime = runtime or legacy_runtime()

    return LegacyServerMcpRuntime(
        require_startup_safety=runtime.require_mcp_startup_safety,
        run_mcp=runtime.run_mcp,
    )


__all__ = ["build_legacy_server_mcp_runtime"]
