# WP1 One-Hundred-Tenth Slice: pure runtime identity extraction

## Boundary moved

The pure `server._runtime_identity_block` prompt renderer now lives as
`sonder_runtime.domain.runtime_identity.runtime_identity_block`. The server
keeps its compatibility name as an import alias, so existing callers and
monkeypatch-based tests retain their behavior.

## Why this is safe

The helper only transforms its explicit `model` and `cloud` arguments into
text. It performs no I/O, reads no environment or tier table, and has no
mutable state. In particular, an unknown model still emits no identity claim.

## Verification

- Focused domain and server identity tests pass.
- Compile, architecture, requirement-evidence, and staged/working diff gates
  pass.
- Persistence, command catalog, launchers, HTTP/REPL, and `unsafe_lab` paths
  are outside this slice.
