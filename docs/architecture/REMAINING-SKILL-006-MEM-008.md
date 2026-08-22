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

Evidence:

- `tests/test_remaining_procedural_publication.py`
- focused command: `python -m pytest -q tests/test_remaining_procedural_publication.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime`
- `git diff --check`

Formal checklist checkboxes remain unchanged; this is an isolated contract
slice and does not claim end-to-end persistence integration.
