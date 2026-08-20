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

The catalog has no hidden I/O.  `CatalogSnapshot` is the persistence seam: a
host can atomically store and restore it, and restoration verifies its
deterministic integrity digest.  This keeps the contract testable while
avoiding a false claim that process memory itself is durable storage.

Evidence:

- `tests/test_remaining_procedural_publication.py`
- focused command: `python -m pytest -q tests/test_remaining_procedural_publication.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime`
- `git diff --check`

Formal checklist checkboxes remain unchanged; this is an isolated contract
slice and does not claim end-to-end persistence integration.
