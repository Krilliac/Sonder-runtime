"""Compatibility adapter for injected command/runtime hooks.

The legacy ``server`` module is intentionally not discovered here.  A
composition boundary supplies its hooks explicitly, keeping this adapter
testable and preventing an accidental import-time dependency on the legacy
root.
"""
from __future__ import annotations

from collections.abc import Callable

from ..domain.common.errors import DependencyUnavailable


StartupSafetyHook = Callable[[], None]
McpRunHook = Callable[..., None]


class LegacyServerMcpRuntime:
    """Adapt explicitly injected legacy startup and MCP hooks.

    Missing hooks are a configuration error, not permission to discover the
    legacy root dynamically.  Failing before any work starts preserves the
    startup safety boundary.
    """

    def __init__(
        self,
        *,
        require_startup_safety: StartupSafetyHook | None = None,
        run_mcp: McpRunHook | None = None,
    ) -> None:
        self._require_startup_safety = require_startup_safety
        self._run_mcp = run_mcp

    def require_startup_safety(self) -> None:
        hook = self._require_startup_safety
        if hook is None:
            raise DependencyUnavailable(
                "MCP runtime requires an injected startup-safety hook"
            )
        hook()

    def run(self, *, safety_checked: bool) -> None:
        hook = self._run_mcp
        if hook is None:
            raise DependencyUnavailable("MCP runtime requires an injected run hook")
        hook(safety_checked=safety_checked)


__all__ = ["LegacyServerMcpRuntime"]
