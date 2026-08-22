# WP1 Two-Hundred-Eightieth Slice — native MCP read-only tool parity

## Boundary

Added six guarded read-only filesystem/workbench tools to the opt-in native
MCP surface: `directory_tree`, `file_find`, `file_read_range`,
`program_search`, `script_search`, and `text_search`. They execute through the
packaged `ToolExecutorAdapter`, preserve bounded adapter evidence, and expose
explicit JSON schemas. Native catalog construction is now name-sorted for
stable `tools/list` output.

## Evidence

- Native MCP, stdio, and typed executor regressions pass: **22 passed**.
- The native catalog reports **16** deterministic names; the legacy source
  audit remains **204** registered MCP tools.
- Read-only search/range/tree calls were exercised against a temporary guarded
  workspace; malformed native arguments remain protocol errors.
- `git diff --check` and the architecture gate pass.

## Limitation

This is a bounded MCP parity slice. The historical catalog remains the default
compatibility path; the remaining legacy tools, full MCP parity, epoch-2 bridge
retirement, and formal checklist acceptance remain incomplete.
