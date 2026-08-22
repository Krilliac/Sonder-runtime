# WP1 Two-Hundred-Eighty-Third Slice — native MCP inspection service parity

## Boundary

Connected nine existing packaged read-only inspection tools to the native MCP
application graph: `archive_list`, `data_inspect`, `data_query`,
`dependency_inventory`, `directory_digest`, `file_digest`, `log_inspect`,
`project_detect`, and `workspace_compare`. Native calls now route through
`InspectionService` and its `InspectionExecutorAdapter`, with explicit
required-field schemas and fresh MCP operation contexts.

## Evidence

- Native MCP, stdio, inspection facade, and typed routing regressions pass:
  **21 passed**.
- A native `file_digest` call was verified to reach the application inspection
  service rather than the mutating `ToolExecutor` port.
- The native catalog now reports **31** deterministic names against the legacy
  source audit's **204** registered MCP tools.
- `git diff --check` and the architecture gate pass.

## Limitation

This slice covers the existing packaged inspection service only. Full MCP
parity, remaining legacy tool families, epoch-2 bridge retirement, and formal
checklist acceptance remain incomplete.
