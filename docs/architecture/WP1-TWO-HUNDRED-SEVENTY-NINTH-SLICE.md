# WP1 Two-Hundred-Seventy-Ninth Slice — native MCP filesystem alias parity

## Boundary

Expanded the opt-in native MCP catalog from six canonical application tools to
ten public names. Five legacy filesystem/workbench names are now explicitly
represented in the native catalog: `directory_create`, `file_edit`,
`file_read`, `file_write`, and `workspace_run`. Their calls are schema-checked
at the MCP boundary and normalized to the existing typed `ToolExecutor` names;
no legacy server import or direct filesystem execution was added.

## Evidence

- Native MCP and stdio protocol/catalog regressions pass: **25 passed**.
- The legacy source audit reports **204** registered MCP tools; the native
  catalog now reports **10** names, including the five migrated aliases.
- Invalid alias arguments fail as JSON-RPC `-32602` before executor entry.
- `git diff --check` and the architecture gate pass.

## Limitation

This is a bounded MCP parity slice. The historical 204-tool server catalog
remains the default compatibility path; full MCP parity, epoch-2 bridge
retirement, and formal checklist acceptance remain incomplete.
