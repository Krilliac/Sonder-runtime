# WP1 Two-Hundred-Seventy-Seventh Slice — complete cloud-policy caller migration

## Boundary

Migrated the final production cloud-policy callers: the admin diagnostic
display and ensemble target selection. Together with the preceding transport,
gateway, routing, fanout, status, and generation slices, the root
`cloud_allowed()` function is now compatibility-only.

## Evidence

- A global source audit shows `cloud_allowed()` has no production call sites;
  only its compatibility definition remains.
- Ensemble, cloud-access, admin, improvement-report, and server-helper
  regressions pass: **104 passed**.
- `server.py` compiles; `git diff --check` and the architecture gate pass.

## Limitation

This completes the cloud-policy caller seam only. REPL compatibility,
MCP parity, epoch-2 bridge retirement, and formal checklist acceptance remain
incomplete.
