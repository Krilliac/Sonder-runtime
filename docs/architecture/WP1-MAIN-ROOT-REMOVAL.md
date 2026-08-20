# WP1 Main Entry-Point Root Removal

## Boundary

`sonder_runtime/__main__.py` is the packaged command entrypoint.  It must not
depend directly on the historical root `server` module or use a dynamic import
to evade the architecture boundary.

## Implementation

- `sonder_runtime/application/command_surface/mcp.py` defines the typed
  `McpRuntime` port and `McpCommand` orchestration facade.
- `sonder_runtime/adapters/command_surface.py` is the sole compatibility
  adapter for the legacy server MCP hooks.
- `sonder_runtime/__main__.py` delegates through `McpCommand` and preserves the
  existing ordering: startup safety, configuration/environment export, then
  `run_mcp(safety_checked=True)`.

The application facade is root-free.  No formal specification checkboxes are
changed by this migration slice.

## Evidence

- `tests/test_wp1_main_root_removal.py` checks the AST for direct or dynamic
  root imports and proves ordering, failure short-circuiting, and hook
  delegation.
- `python -m pytest tests/test_wp1_main_root_removal.py -q`
- `python scripts/check_architecture.py`
