# Native MCP transport migration slice

Status: implemented on `agent/wp1-execution-status` as an explicit opt-in
surface (`python -m sonder_runtime mcp --native`).

## Scope

The native path composes the bounded JSON-RPC stdio transport with the
application-owned `ToolExecutor` port and a deterministic generated catalog.
The catalog currently contains the six canonical `ToolExecutorAdapter` tools,
five explicitly-scoped filesystem/workbench aliases (`directory_create`,
`file_edit`, `file_read`, `file_write`, and `workspace_run`), and six guarded
read-only inspection/search tools (`directory_tree`, `file_find`,
`file_read_range`, `program_search`, `script_search`, and `text_search`).
Alias calls normalize to canonical executor names, and the native boundary
validates their JSON schemas before execution. Each call gets a fresh MCP
`OperationContext`, and executor results retain bounded error and evidence
fields.

Nine packaged read-only inspection tools are also exposed through the native
application inspection port: `archive_list`, `data_inspect`, `data_query`,
`dependency_inventory`, `directory_digest`, `file_digest`, `log_inspect`,
`project_detect`, and `workspace_compare`. They are dispatched through
`InspectionService`/`InspectionExecutorAdapter`, preserving the existing
read-only policy and evidence shape rather than duplicating inspection logic.

The native catalog also includes four guarded mutation tools: `file_copy`,
`file_move`, `file_batch_write`, and `file_delete`. These use the packaged
transfer, transactional batch, and explicit-delete-confirmation adapters;
they do not accept legacy authentication tokens on the native surface.

The catalog now also exposes `json_patch` and `text_patch` through the typed
executor. Their guarded implementations are packaged under
`sonder_runtime/adapters/filesystem/`, preserving transactional reports and
rollback behavior without adding a root-module import to the native path. The
historical root modules remain only as compatibility entrypoints for the
legacy server surface.

The historical server MCP catalog remains the default compatibility path until
catalog parity and complete application-service coverage are proven. The
legacy catalog currently contains 204 registered tools; the native catalog
therefore covers 31 names and does not claim full parity, API-003, or TOOL-001
completion.

## Evidence

- `tests/test_native_mcp.py`: deterministic catalog, alias normalization,
  schema rejection, and end-to-end transport to application tool-port
  translation.
- `tests/test_mcp_stdio_transport.py`: negotiation, bounded frames,
  subscriptions, malformed input, and catalog limits.
- Focused result: **38 passed**.
- `scripts/check_architecture.py`, compileall, and diff checks pass.
