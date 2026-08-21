# WP1 Two-Hundred-Seventieth Slice — tier-discovery cloud policy migration

## Boundary

Rewired `available_tiers()` to invoke the packaged cloud opt-in policy
directly while retaining the existing live cloud-tier refresh and
`include_disabled` behavior. The root `cloud_allowed()` helper remains as a
compatibility delegate for callers that still need its zero-argument contract.

## Evidence

- An AST regression test proves `available_tiers()` contains no call to the
  root cloud-policy wrapper.
- Cloud-access, cloud-routing, specialist-tier, tier-name, capability-router,
  and ensemble regressions pass: **76 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Serve-target, gateway, REPL, and other cloud-policy callers remain staged for
later migration. MCP parity, epoch-2 bridge retirement, and formal checklist
acceptance remain incomplete.
