# Native MCP legacy file-edit slice

## Boundary

`file_edit` is a legacy native-MCP alias for the packaged `edit_file`
implementation already routed by the typed `ToolExecutor` port. The native
descriptor requires a path and replacement strings, bounds the optional
replacement count, and rejects legacy token or approval bypass fields.

The existing alias route normalizes `file_edit` to `edit_file`; no adapter or
`tool_executor.py` changes are part of this slice.

## Evidence

- `tests/test_native_mcp.py` verifies deterministic catalog exposure, bounded
  schema fields, bypass-field omission, and routing through the typed executor.
