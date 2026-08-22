# WP1 Two-Hundred-Seventy-First Slice — parallel generation cloud policy

## Boundary

Rewired `parallel_generate_run` and
`parallel_generate_run_languages` to consult the packaged cloud opt-in policy
directly before selecting a hosted tier. Existing generation bounds and cloud
disabled responses remain unchanged.

## Evidence

- AST regression tests prove both parallel-generation paths contain no call to
  the root `cloud_allowed()` wrapper.
- Parallel-generation, cloud-routing, gateway, and ensemble regressions pass:
  **94 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Serve-target, gateway context injection, REPL, and fanout cloud-policy callers
remain staged for later migration. MCP parity, epoch-2 bridge retirement, and
formal checklist acceptance remain incomplete.
