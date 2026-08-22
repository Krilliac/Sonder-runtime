# WP1 Two-Hundred-Seventy-Fourth Slice — gateway cloud-consent migration

## Boundary

Rewired `_gateway_generate_text` to populate `local_owner_context` from the
packaged cloud opt-in policy directly. The gateway’s remote-endpoint and
timeout context fields remain unchanged; tests now inject the packaged policy
seam rather than the legacy root wrapper.

## Evidence

- An AST regression test proves `_gateway_generate_text` contains no call to
  the root `cloud_allowed()` wrapper.
- Gateway, cloud-routing, conformance, OpenAI-compatibility, offload, and
  server-helper regressions pass: **135 passed, 5 skipped**.
- `git diff --check` and the architecture gate pass.

## Limitation

REPL and remaining status/reporting cloud callers remain staged. MCP parity,
epoch-2 bridge retirement, and formal checklist acceptance remain incomplete.
