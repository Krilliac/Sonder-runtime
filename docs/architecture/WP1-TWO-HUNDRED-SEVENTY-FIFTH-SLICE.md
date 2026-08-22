# WP1 Two-Hundred-Seventy-Fifth Slice — status/report cloud policy migration

## Boundary

Rewired the `status`, `learn_tiers`, and `improvement_report_data` surfaces to
use the packaged cloud opt-in policy directly for tier display, learning
reporting, and deployment health signals. Existing status/report shapes and
privacy wording remain unchanged.

## Evidence

- AST regression tests prove all three surfaces contain no call to the root
  `cloud_allowed()` wrapper.
- Cloud-access, model-inventory/status, server-helper, learning-health, and
  serve-auth regressions pass: **87 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

REPL and a few lower-level model/provider cloud checks remain staged. MCP
parity, epoch-2 bridge retirement, and formal checklist acceptance remain
incomplete.
