# Evidence roadmap checkpoint — 2026-08-21

Status: checkpoint only. This file records the bounded audit result and next
implementation cluster. It does not modify the master specification,
`docs/architecture/evidence/requirements.jsonl`, or generated status files.

## Formal baseline

- Remaining unproven formal rows: 42 — `CORE-001`–`CORE-010`,
  `ARCH-001`–`ARCH-013`, `COMPACT-001`–`COMPACT-005`, `EXT-001`–`EXT-007`,
  `SELFMOD-001`–`SELFMOD-006`, and `API-003`.
- Formal ledger: 204 latest records, all `planned`; 0 `verified`.
- Master checklist: 0 of 204 checked.
- Existing scoped audit: 162 `PROVEN-CONTRACT`, 1 `PARTIAL` (`API-003`),
  0 `MISSING`; this is contract evidence, not formal completion evidence.

## Bounded checks completed

From repository root:

| Check | Exact command | Result |
|---|---|---|
| Formal requirement/ledger gate | `python scripts/check_requirement_evidence.py` | PASS, exit 0 |
| Evidence-document gate | `python scripts/check_evidence_documents.py` | PASS, exit 0 |
| Documentation authority | `python scripts/check_documentation_authority.py` | PASS, exit 0 |
| Generated documentation | `python scripts/generate_documentation_catalogs.py --check` | PASS, exit 0 |
| Architecture gate | `python scripts/check_architecture.py` | PASS, exit 0 |

The focused implementation batch was started with:

`python -m pytest -q --basetemp .pytest-evidence-roadmap-20260821 tests/test_wp4_compact001_005.py tests/test_crosscutting_extensions.py tests/test_remaining_selfmod_governance.py tests/test_remaining_session_durable_replay.py tests/test_session_repository.py tests/test_composition_job_registry.py tests/test_crosscutting_provider_lifecycle.py tests/test_provider_lifecycle.py tests/test_api003_subprocess_provider.py tests/test_api003_legacy_declaration.py tests/production/test_composition_root.py tests/production/test_architecture.py`

It was intentionally interrupted at the user checkpoint after progress passed
the 57% display mark. It has no test-pass/fail conclusion and must not be used
as evidence.

## Evidence files inspected

- `docs/architecture/evidence/REQUIREMENT-AUDIT-2026-08-21-POST-PDF-PROCESS-ARTIFACT-OWNERSHIP.md`
- `docs/architecture/REQUIREMENT-AUDIT-2026-08-21.md`
- `docs/architecture/REQUIREMENT-AUDIT-NEXT.md`
- `docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md`
- `docs/architecture/evidence/requirements.jsonl`
- `docs/architecture/generated/requirement-status.json`
- `docs/architecture/DOCUMENT-AUTHORITY-INDEX.md`
- `docs/architecture/README.md`

## Recommended next cluster

### Durable session integration and recovery — bounded slice completed

The first production compaction slice is now wired through the canonical
application graph. `SessionCompactionService` reads an exact durable source
range, invokes typed compaction and factual-retention validation, appends one
structured `compaction.completed` event, and proves restart visibility.

Evidence: `docs/architecture/evidence/COMPACTION-PRODUCTION-INTEGRATION-2026-08-21.md`.

The broader end-to-end acceptance cluster remains open:

Continue the production composition path that connects the existing session
repository, event vocabulary, replay/query/export, checkpoints, and privacy
contracts to the live HTTP/MCP/REPL turn lifecycle. The cluster should include
restart-visible session state, durable compaction events, checkpoint/replay
binding, bounded query/export, retention execution, and recovery evidence.

Primary formal rows advanced: `SESSION-001`–`SESSION-010`, `LOOP-001`–`LOOP-008`,
`SEAM-006`–`SEAM-007`, `COMPACT-001`–`COMPACT-005`, and supporting
`DATA-003`/`DATA-004`/`OPS-004` rows. It also supplies the strongest evidence
substrate for `CORE-005`, `ARCH-001`, `ARCH-005`, `ARCH-006`, and `ARCH-007`.

### Bounded extension isolation — implementation slice completed

`sonder_runtime/adapters/extensions/host.py` now provides a bounded
JSON-lines child-process host with startup/call deadlines, output limits, and
restart/crash budgets. Focused evidence is recorded in
`docs/architecture/evidence/EXT-003-BOUNDED-EXTENSION-HOST-2026-08-21.md`.
The formal EXT-003 row remains unverified because native memory limiting,
manifest admission, and production registry wiring are still open.

The adjacent EXT-004/005 registry slice is also implemented in
`sonder_runtime/application/extensions/registry.py`, with project/global
state, quarantine, disablement, update records, and repair diagnostics. Its
formal rows remain open pending durable storage and CLI/API/UI exposure.

Acceptance evidence required before any formal promotion:

1. One live composition-root test starts a turn, persists the complete durable
   event sequence, restarts, and reconstructs the same request/transcript/UI
   projection.
2. A compaction/recovery rehearsal proves append-only source history, exact
   source range, typed modality retention, checkpoint binding, and repair of a
   truncated tail without replaying side effects.
3. HTTP/MCP/REPL surfaces share the same repository port and bounded query/export
   behavior; selected regression, architecture, migration, and recovery checks
   pass from a clean isolated state.
4. Evidence records include baseline and verified SHAs, exact commands, artifact
   paths, and explicit limitations; only then should formal ledger/checklist
   changes be considered in a separate reviewed change.

### Why this outranks jobs/provider lifecycle

Jobs and provider lifecycle already have relatively strong contract slices and
focused tests, while their remaining gaps are mainly broad integration. Durable
sessions are the shared durability substrate for turns, compaction, jobs,
provider events, replay, recovery, and operator-visible history. Closing this
boundary therefore creates reusable end-to-end evidence for more requirements
and reduces ambiguity in later job/provider acceptance tests.

### Follow-on order

1. Durable session integration and recovery.
2. Generic job registry wired to the same durable event/recovery model, including
   process-tree cleanup, output watermarks, spill references, and restart repair.
3. Provider lifecycle production composition, including specialized provider
   publication, cancellation, rollback, and dependency health through live
   model/embedding/training/update paths.
4. Re-run documentation generation after trusted-session staging makes the new
   production files visible to the Git-indexed catalog, then repeat the formal
   evidence and architecture gates.

## Protected scope

This checkpoint does not promote any formal checklist or ledger row. The
bounded compaction and extension-host slices are separately evidenced, while
the formal ledger remains unchanged and conservative.
