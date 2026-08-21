# WP1 Two-Hundred-Seventy-Sixth Slice — provider cloud gate migration

## Boundary

Rewired the immediate model transport gate, offload route gate, and
serialized-answer cache cloud-consent metadata to the packaged cloud opt-in
policy. Provider dispatch remains fail-closed when consent is absent.

## Evidence

- AST regression tests prove `_post_model`, `_offload_impl`, and
  `_sonder_impl` contain no call to the root `cloud_allowed()` wrapper.
- Model-retry, offload-schema, request-cache, server-helper, cloud-routing,
  and gateway regressions pass: **203 passed**.
- `server.py` compiles; `git diff --check` and the architecture gate pass.

## Limitation

The remaining root cloud-policy calls are limited to a diagnostic display and
ensemble target reporting path, plus the compatibility definition. REPL/MCP
parity, epoch-2 bridge retirement, and formal checklist acceptance remain
incomplete.
