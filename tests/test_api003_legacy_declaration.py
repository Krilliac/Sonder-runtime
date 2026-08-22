from __future__ import annotations

from types import SimpleNamespace

from sonder_runtime.bootstrap.legacy_mcp import (
    LEGACY_MCP_DECLARATION,
    build_legacy_server_mcp_runtime,
)
from sonder_runtime.application.protocol.mcp_compatibility import LegacyMcpContract


def test_legacy_mcp_factory_carries_explicit_typed_declaration():
    runtime = SimpleNamespace(
        require_mcp_startup_safety=lambda: None,
        run_mcp=lambda *, safety_checked: None,
    )

    adapter = build_legacy_server_mcp_runtime(runtime)

    assert isinstance(LEGACY_MCP_DECLARATION, LegacyMcpContract)
    assert adapter.declaration is LEGACY_MCP_DECLARATION
    assert adapter.declaration == LegacyMcpContract(
        name="legacy-server", version="1.0", capabilities=("tools",)
    )


def test_injected_legacy_adapter_without_declaration_remains_unconfigured():
    from sonder_runtime.adapters.command_surface import LegacyServerMcpRuntime

    adapter = LegacyServerMcpRuntime(
        require_startup_safety=lambda: None,
        run_mcp=lambda *, safety_checked: None,
    )

    assert adapter.declaration is None
