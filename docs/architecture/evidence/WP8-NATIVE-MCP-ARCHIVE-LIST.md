# Native MCP archive listing slice

## Boundary

`archive_list` is the next low-risk legacy catalog capability after
`archive_create`: it is read-only and already has a packaged
`archive_tools.list_archive` implementation behind the typed
`InspectionExecutorAdapter`. The native descriptor now carries the same
explicit path, archive-size, expansion-ratio, depth, result, and time bounds
as that packaged route. Legacy `token` and `approval` bypass fields are not
accepted.

## Evidence

- `tests/test_native_mcp.py` verifies deterministic catalog exposure, bounded
  schema ceilings, bypass-field omission, and routing through the application
  inspection port.
- No adapter or `tool_executor.py` changes are required for this migration
  slice.
