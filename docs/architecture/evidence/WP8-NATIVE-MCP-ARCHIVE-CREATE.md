# Native MCP archive creation slice

## Boundary

`archive_create` is the next self-contained legacy archive capability after
`archive_extract`. The native descriptor requires a project root, explicit JSON
inputs, and a new destination; it bounds archive format, file and entry counts,
per-file and aggregate bytes, depth, and serialized input size. Legacy token and
approval bypass fields are intentionally absent.

The native transport validates the descriptor and routes the call through the
existing typed `ToolExecutor` port. No legacy server or adapter code is changed
by this slice.

## Evidence

- `tests/test_native_mcp.py` verifies deterministic catalog exposure, bounded
  schema fields, omission of bypass arguments, and application-port routing.
