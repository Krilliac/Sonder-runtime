# ARCH-012 generated ownership integration — 2026-08-21

The typed ownership catalog is now consumed by the authoritative documentation
generator. The generated architecture map carries a deterministic ownership
projection for every current runtime layer, including package, state, public
port, provider, schema, and lifecycle rows. The projection is built from the
layer names already present in the architecture map; it does not invent
filesystem or runtime discovery from the application layer.

Evidence:

- `sonder_runtime/application/architecture/ownership_catalog.py` exposes
  `default_layer_ownership_catalog` with complete validation and stable order.
- `scripts/generate_documentation_catalogs.py` embeds the catalog snapshot in
  `architecture-map.json` and the generated Markdown authority map.
- `tests/test_ownership_catalog.py` covers deterministic layer records and
  invalid inputs.
- `tests/test_architecture_ownership_generation.py` binds the generated JSON
  projection back to the typed catalog.
- `python scripts/generate_documentation_catalogs.py --check` passes.

This closes generated-authority wiring for the layer-level inventory. Full
resource-level production ownership and the formal ARCH-012 checkbox remain
unverified.
