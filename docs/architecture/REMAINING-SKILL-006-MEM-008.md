# SKILL-006 / MEM-008 — durable procedural skill publication

`sonder_runtime.application.skills.procedural_publication` closes the gap
between WP6 candidate/evidence generation and a versioned skill registry.
`HeldOutEvidence` is immutable and digest-bound to the candidate skill; a
publication is accepted only when the evidence passed, the candidate and
revision match, and the existing skill refresh trust/compatibility policy
allows the revision.

`DurableLastGoodCatalog` keeps an append-only revision history and separate
active, last-good, and disabled indexes.  Publishing a new version retains the
previous active version as last-good.  Rollback is explicit and restores that
version; disablement removes the active route, records an operator reason, and
blocks new publication until explicitly enabled.

`ProceduralPublicationService` is the active-skill integration.  It consumes an
approved `MeasuredPromotionGates` decision plus a typed procedural memory,
creates the existing memory-to-promotion linkage, and records both held-out
skill evidence and measured promotion provenance.  A catalog transaction and
an `ActiveSkillPort` snapshot are committed together.  Activation, event, or
catalog failure restores both snapshots and emits only bounded failure
provenance; no partially active skill is left behind.  `rollback()` uses the
same guarded path to restore the last-good revision.

The catalog has no hidden I/O.  `CatalogSnapshot` is the persistence seam: a
repository-owned adapter can atomically store and restore it, and restoration
verifies its deterministic integrity digest.  The application transaction is
therefore usable with the existing memory/promotion ports without opening a
second database or claiming that process memory alone is durable storage.

`CatalogStorePort` names that seam, and
`sonder_runtime.adapters.persistence.sqlite.skill_catalog.SQLiteCatalogSnapshotStore`
implements it as one generation-counted SQLite row holding the snapshot's
canonical JSON and digest.  `save` refuses a snapshot that does not verify and
replaces the row in one `BEGIN IMMEDIATE` transaction; `load` returns `None`
for an empty store and otherwise rebuilds the snapshot through
`DurableLastGoodCatalog.from_snapshot`, so a tampered payload, a wrong digest,
or malformed JSON raises `CatalogStoreError` and nothing is restored.  Each
store instance remembers the generation it last loaded or saved, and `save`
refuses with `CatalogStoreError` inside the same `BEGIN IMMEDIATE`
transaction when another instance or process wrote the row since, so two
compositions over one file cannot silently overwrite each other; the refused
service rolls back and the host must reopen the composition to continue.  The
digest detects corruption and uncoordinated edits; it is not an authenticity
signature against a writer able to recompute SHA-256.

`build_procedural_publication_composition(store=..., active=...)` restores the
catalog from the store, re-activates each catalog-active revision in the
injected `ActiveSkillPort`, and hands the store to
`ProceduralPublicationService`.  Publish, rollback, `disable`, and `enable`
then save the staged snapshot as the last step inside the guarded catalog
transaction: a failed save restores the in-memory catalog and the active-skill
snapshot exactly like any other failure.  `ActiveSkillPort` has no
deactivation seam, so a disabled skill is withdrawn from `catalog.current()`
but a host's active port keeps its last activation until the host consults the
catalog.

Evidence:

- `tests/test_remaining_procedural_publication.py`
- `tests/test_mem008_procedural_composition.py`
- `tests/test_mem008_procedural_catalog_sqlite.py` (publish, reopen, and
  rollback over a real SQLite file; tampered and malformed rows fail closed;
  an injected save failure leaves catalog and active port unchanged; a second
  writer on the same file is refused and rolled back)
- focused command: `python -m pytest -q tests/test_remaining_procedural_publication.py tests/test_mem008_procedural_composition.py tests/test_mem008_procedural_catalog_sqlite.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime`
- `git diff --check`

Formal checklist checkboxes remain unchanged.  The durable store is an
application-composition capability only: no runtime path publishes procedural
skills or composes the catalog in `bootstrap/`, so this does not claim
end-to-end bootstrap persistence integration.
