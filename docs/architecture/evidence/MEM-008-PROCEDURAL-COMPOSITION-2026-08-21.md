# MEM-008 procedural publication composition — 2026-08-21

This slice adds the smallest typed application graph for the existing
procedural-memory publication contracts. `build_procedural_publication_composition`
injects the host-owned catalog and active-skill ports, applies the existing
procedural memory admission policy, and delegates publication to
`ProceduralPublicationService`.

The integration preserves the existing safeguards rather than duplicating
them: held-out evidence remains candidate- and digest-bound, memory-to-skill
provenance includes source interactions and rollback references, and catalog
plus active-skill state still use the service's atomic snapshot/restore path.
Missing source provenance is rejected before catalog mutation, and
non-procedural memory is rejected by the publication linkage contract.

The default catalog is explicitly an in-process reference adapter. A durable
host must inject a catalog restored from a verified `CatalogSnapshot`; this
slice does not claim persistence or activate a second store.

Evidence:

- `tests/test_mem008_procedural_composition.py`
- `python -m pytest -q tests/test_mem008_procedural_composition.py tests/test_remaining_procedural_publication.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime tests`
- `git diff --check`
