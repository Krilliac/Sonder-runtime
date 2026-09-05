"""Legacy MCP composition boundary.

Only this bounded bootstrap module resolves the historical ``server`` root.
The application command and its adapter receive plain callable dependencies.
"""
from __future__ import annotations

from types import ModuleType

from sonder_runtime.adapters.command_surface import LegacyServerMcpRuntime
from sonder_runtime.application.protocol.mcp_compatibility import LegacyMcpContract
from .legacy_root import runtime_proxy


LEGACY_MCP_DECLARATION = LegacyMcpContract(
    name="legacy-server",
    version="1.0",
    capabilities=("tools",),
)


def configure_legacy_application(application) -> None:
    """Bind the owned Application through the existing MCP bootstrap seam."""
    from .legacy_root import configure_application

    configure_application(application)


def build_legacy_server_mcp_runtime(
    runtime: ModuleType | None = None,
) -> LegacyServerMcpRuntime:
    """Compose the legacy MCP hooks at the process boundary."""
    runtime = runtime or runtime_proxy()

    def require_startup_safety() -> None:
        runtime.require_mcp_startup_safety()

    def run_mcp(*, safety_checked: bool) -> None:
        runtime.run_mcp(safety_checked=safety_checked)

    return LegacyServerMcpRuntime(
        require_startup_safety=require_startup_safety,
        run_mcp=run_mcp,
        declaration=LEGACY_MCP_DECLARATION,
    )


__all__ = ["LEGACY_MCP_DECLARATION", "build_legacy_server_mcp_runtime", "configure_legacy_application"]
