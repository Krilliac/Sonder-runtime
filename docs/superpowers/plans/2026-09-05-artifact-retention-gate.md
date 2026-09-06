# Reference-Aware Artifact Cache Retention Implementation Plan

> **For agentic workers:** This bounded slice is implemented with test-first development and a pure domain boundary.

**Goal:** Decide whether content-addressed cache entries may be retained, deleted, or deferred without touching storage, while live job/deployment references and durable tombstones fence unsafe cleanup.

**Architecture:** Add `sonder_runtime.domain.artifact_retention`, a storage-neutral policy over immutable cache entries, reference snapshots, and tombstone snapshots. The policy never opens files, databases, or network connections; adapters may consume its deterministic plan later. Every identity is bound by artifact ID, SHA-256 digest, and version, and incomplete bounded scans defer cleanup.

**Tech Stack:** Python dataclasses, `enum.StrEnum`, timezone-aware `datetime`, pytest.

**Spec:** Parent roadmap task, P5 artifact mobility safety / reference-aware retention and garbage collection.

## Global Constraints

- No I/O, persistence, networking, deletion, or automatic promotion in the domain module.
- Deletion is only planned after complete bounded scans, exact digest/version checks, no live owner reference, and elapsed retention age.
- Unknown, conflicting, stale, or truncated metadata must defer cleanup with a stable reason.
- New tests must demonstrate RED before implementation and GREEN after implementation.

### Task 1: Define the failing contract tests

**Files:**
- Create: `tests/test_artifact_retention.py`

- [ ] Add tests for live job/deployment retention, retention windows, exact identity mismatch deferral, deletion and retention tombstones, bounded incomplete scans, deterministic ordering, and input validation.
- [ ] Run `pytest -q tests/test_artifact_retention.py` and observe import/API failures before implementation.

### Task 2: Implement the pure retention policy

**Files:**
- Create: `sonder_runtime/domain/artifact_retention.py`

- [ ] Implement bounded immutable records and explicit decision enums.
- [ ] Implement deterministic `plan_artifact_gc` with fail-closed ordering and generated deletion tombstones.
- [ ] Run the focused tests and refactor only after GREEN.

### Task 3: Document the contract and refresh generated authority

**Files:**
- Create: `docs/architecture/P5-ARTIFACT-RETENTION-GATE.md`
- Create: `docs/runbooks/artifact-cache-retention.md`
- Modify: generated documentation catalogs via `scripts/generate_documentation_catalogs.py --write`

- [ ] State adapter responsibilities, bounds, and guarantees/limits.
- [ ] Run architecture, documentation, and diff checks.

### Task 4: Commit and publish

- [ ] Run focused and relevant regression tests with fresh output.
- [ ] Commit only targeted paths with DCO sign-off.
- [ ] Push `codex/p5-artifact-gc-gate` and open a reviewable PR with auto-merge enabled when checks are eligible.
