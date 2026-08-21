# WP1 Two-Hundred-Eighty-Fifth Slice — native MCP process-risk parity

## Boundary

Ported the bounded, read-only process-risk implementation into
`sonder_runtime/adapters/process_risk.py` and exposed `process_list` plus
`process_memory_risk_inspect` through the typed native MCP executor. The
explicit `SONDER_PROCESS_INSPECTION=enabled:bounded-read-only` gate and
content-free risk contract remain unchanged.

## Evidence

- Native MCP, typed executor, process-risk, server compatibility, and stdio
  regressions pass: **54 passed**.
- The packaged adapter was verified to refuse process inventory without the
  explicit opt-in environment value.
- The native catalog now reports **34** deterministic names against the legacy
  source audit's **204** registered MCP tools.
- `git diff --check` and the architecture gate pass.

## Limitation

Artifact-risk inspection still depends on a root-owned PDF risk module and is
not included in this slice. Vision, remaining legacy tool families, epoch-2
bridge retirement, and formal checklist acceptance remain incomplete.
