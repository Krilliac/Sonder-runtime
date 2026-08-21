# WP1 Two-Hundred-Ninetieth Slice — native redacting secret scan

## Boundary

Packaged the workspace secret scan around the existing redacting
`CredentialScanner`. The adapter enforces guarded root resolution, one-megabyte
file bounds, binary suffix exclusions, a 120-second ceiling, a 100-finding
limit, and timeout truncation. It never returns matched credential material.
Native MCP exposes `secret_scan` through the typed executor with only root and
timeout arguments; legacy tokens and unbounded harness controls are absent.

## Evidence

- Packaged scanner and timeout/redaction tests pass: **2 passed**.
- Native executor/catalog regression is covered alongside the migration suite.
- The native catalog now reports **39** names against the legacy source audit's
  **204** registered MCP tools.

## Limitation

Full legacy MCP parity and formal checklist acceptance remain incomplete.
