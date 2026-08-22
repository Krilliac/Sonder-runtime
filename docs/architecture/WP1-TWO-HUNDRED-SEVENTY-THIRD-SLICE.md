# WP1 Two-Hundred-Seventy-Third Slice — serve-target cloud policy migration

## Boundary

Rewired `_serve_target`, the central explicit tier/model routing boundary, to
consult the packaged cloud opt-in policy directly for hosted tiers and
discovered hosted models. Existing live-tier refresh, exact catalog membership,
and cloud-disabled routing results remain unchanged.

## Evidence

- An AST regression test proves `_serve_target` contains no call to the root
  `cloud_allowed()` wrapper.
- Cloud-access, cloud-internal-routing, specialist-tier, server-helper, and
  serve-auth regressions pass: **121 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Gateway context injection, REPL, and remaining status/reporting cloud callers
remain staged. MCP parity, epoch-2 bridge retirement, and formal checklist
acceptance remain incomplete.
