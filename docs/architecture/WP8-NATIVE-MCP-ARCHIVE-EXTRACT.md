# Native MCP archive extraction slice

## Boundary

`archive_extract` is exposed in the native MCP catalog with an explicit
source/destination contract and hard schema ceilings matching the guarded
archive implementation: entries, per-file bytes, total bytes, expansion
ratio, path depth, result count, and execution seconds are all bounded.

The native transport validates the descriptor and dispatches the call through
the existing typed `ToolExecutorAdapter`. No archive implementation or
transport-specific extraction logic is duplicated, and the existing legacy
compatibility aliases remain unchanged.

## Evidence

- `tests/test_native_mcp.py` verifies deterministic catalog exposure, safe
  schema bounds, and routing to the typed executor.
- `tests/test_archive_extract_executor.py` verifies transactional ZIP
  extraction and fail-closed no-replace behavior at the executor boundary.
