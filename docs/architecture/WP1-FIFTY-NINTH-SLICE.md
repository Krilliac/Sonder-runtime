# WP1 Fifty-Ninth Slice — Typed Cloud-Default Caller

## Change

The live-server compatibility repair for the retired `gpt-oss:120b-cloud`
general-tier binding now takes its replacement from the frozen
`RuntimeModelConfiguration` projection. The caller no longer duplicates the
configured general-cloud default as a string literal.

## Contract preserved

The repair remains explicitly mutable: it changes the live `TIERS` mapping only
when the legacy binding is present and the preservation opt-out is absent.
Runtime-policy refresh behavior, policy-file ownership, and all tier routing
semantics remain unchanged.

## Boundary

This is one server caller migration from the typed-configuration projection.
It does not change command catalog, persistence, launchers, HTTP/REPL, or
strangler services.

## Evidence

- Focused server-helper tests prove the legacy repair uses the projection's
  configured replacement and preserves the explicit preservation opt-out.
- Compilation, architecture, requirement-evidence, and staged/working diff
  checks pass.
