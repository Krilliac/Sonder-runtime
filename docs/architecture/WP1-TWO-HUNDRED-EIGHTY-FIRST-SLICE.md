# WP1 Two-Hundred-Eighty-First Slice — native MCP guarded mutation parity

## Boundary

Added four guarded legacy filesystem mutation tools to the opt-in native MCP
surface: `file_copy`, `file_move`, `file_batch_write`, and `file_delete`.
They route through `ToolExecutorAdapter` to the existing transfer,
transactional batch-write, and explicit-delete-confirmation primitives. Native
schemas reject legacy token/approval fields and preserve the adapter evidence
needed for confirmation and rollback reporting.

## Evidence

- Native MCP, stdio, and typed executor regressions pass: **23 passed**.
- Copy, move, batch commit, and delete preview were exercised in a guarded
  temporary workspace; move and batch results preserve their expected state.
- The native catalog now reports **20** deterministic names against the legacy
  source audit's **204** registered MCP tools.
- `git diff --check` and the architecture gate pass.

## Limitation

JSON patch and unified text patch remain separate migration slices because they
have distinct transactional contracts. Full MCP parity, epoch-2 bridge
retirement, and formal checklist acceptance remain incomplete.
