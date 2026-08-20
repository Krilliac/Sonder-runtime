# WP1 command-surface root removal

## Boundary

`sonder_runtime.adapters.command_surface.LegacyServerMcpRuntime` is now a
dependency-only compatibility adapter.  It does not import or discover the
flat `server` composition root.  Startup safety and MCP execution are supplied
as explicit callable hooks.

## Composition

`sonder_runtime.bootstrap.legacy_mcp.build_legacy_server_mcp_runtime` is the
single bounded process-composition boundary that resolves the legacy hooks and
injects them into the adapter.  The application `McpCommand` continues to own
the safety → configuration → run ordering.

If either hook is absent, the adapter raises `DependencyUnavailable` before
performing the missing operation.  This preserves fail-closed behavior and
prevents an implicit legacy-root fallback.

## Evidence

`tests/test_wp1_command_surface_root_removal.py` verifies the adapter's AST has
no direct `server` or dynamic-import bypass, injected hook behavior and order,
fail-closed missing-hook behavior, and bounded entrypoint composition.  The
existing entrypoint contract test was updated to supply the hooks explicitly.

Formal master-spec checkboxes remain intentionally unchanged.
