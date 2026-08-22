# REMAINING-TOOL-006 — Generated catalog foundation

## Contract

`sonder_runtime.application.tools.generated_catalogs.GeneratedCatalogs` is a
transport-neutral projection of the typed application tool registry and typed
durable-event contracts. It produces deterministic MCP, OpenAI function,
CLI, and client catalog shapes from one canonical source. A SHA-256 digest of
that canonical source is included in the client view as a freshness marker.

## Invariants

- Tool and event ordering is canonical and independent of registry iteration order.
- Schemas are copied into bounded, JSON-compatible projections; the generator
  does not import a provider, transport, legacy command registry, or perform I/O.
- Limits are explicit and fail closed with `CatalogLimitError`; output is never
  silently truncated.
- The digest changes when typed tool/event/command contracts change and is
  stable for equivalent input.

## Evidence

`tests/test_remaining_tool_catalogs.py` covers all four projections, event
schema derivation, deterministic freshness, and bounded failure behavior.

Formal checklist checkboxes are intentionally unchanged.
