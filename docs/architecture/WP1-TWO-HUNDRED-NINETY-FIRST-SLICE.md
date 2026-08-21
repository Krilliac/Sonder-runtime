# WP1 Two-Hundred-Ninety-First Slice — consent-gated native web fetch

## Boundary

Packaged `web_fetch` behind a typed executor adapter. It retains the legacy
SSRF-safe public URL validation, response decoding, byte/text bounds, and
block-page detection, while the application operation context now requires
explicit cloud consent. Native MCP requires a boolean `consent` field and
does not accept credentials, headers, bypasses, or arbitrary transport knobs.
Response bodies are returned to the caller but excluded from durable evidence.

## Evidence

- Web adapter consent and block-page tests pass: **2 passed**.
- Native executor/catalog consent regression is covered in the migration suite.
- The native catalog now reports **40** names against the legacy source audit's
  **204** registered MCP tools.

## Limitation

Search, weather, location, and full legacy MCP parity remain separate
migrations. Formal checklist acceptance remains incomplete.
