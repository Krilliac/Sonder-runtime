# WP1 Two-Hundred-Eighty-Seventh Slice — native MCP artifact acquisition

## Boundary

Ported the verified artifact acquisition implementation into
`sonder_runtime.adapters.artifact_fetch`. Native MCP now exposes
`verify_artifact` and `fetch_artifact` through the typed executor. The adapter
preserves SSRF-safe address pinning, the `SONDER_WEB_TOOLS` network-enable
gate, guarded destination containment, atomic partial-file replacement,
provenance sidecars, type/magic checks, digest checks, and optional publisher
verification. Native schemas intentionally omit legacy token and bypass
arguments.

## Evidence

- Native catalog and packaged acquisition regressions pass: **66 passed**.
- The packaged verifier accepted a guarded PE-shaped fixture through the
  typed executor, and the existing acquisition suite remains green.
- The native catalog now reports **37** deterministic names against the
  legacy source audit's **204** registered MCP tools.
- Architecture, compileall, and `git diff --check` gates pass.

## Limitation

Model-backed `vision_analyze` remains a separate local-model policy/service
migration. Full MCP parity, epoch-2 bridge retirement, and formal checklist
acceptance remain incomplete.
