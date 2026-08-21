# WP1 Two-Hundred-Eighty-Second Slice — native MCP patch boundary

## Boundary

Added `json_patch` and `text_patch` to the typed native MCP catalog with
explicit schemas for preview/apply modes, bounded operation input, unified
diff roots, and native extra-root handling. Their implementations now live in
the packaged filesystem adapter; the executor normalizes the legacy
`operations_json` MCP spelling to the patch implementation's `operations`
parameter and returns machine-readable transaction reports.

## Evidence

- Native MCP, typed executor, JSON Patch, unified text patch, and server
  compatibility regressions pass: **61 passed, 3 skipped**.
- JSON Patch apply followed by a transactional unified-diff apply was exercised
  in a guarded temporary workspace; final content and `applied` reports were
  verified.
- The native catalog now reports **22** deterministic names against the legacy
  source audit's **204** registered MCP tools.
- `git diff --check` and the architecture gate pass.

## Limitation

The legacy root modules remain as server compatibility entrypoints and are not
yet retired. Full MCP parity, epoch-2 bridge retirement, and formal checklist
acceptance remain outstanding.
