# P5 — Reference-aware artifact cache retention gate

## Boundary

`sonder_runtime.domain.artifact_retention` is the pure decision boundary for
content-addressed artifact cache cleanup. It consumes immutable cache entries
and bounded pages of owner references and lifecycle tombstones. It returns a
bounded `ArtifactGcPlan`; it does not open SQLite, inspect a filesystem path,
contact a node, delete bytes, or mutate a tombstone store.

An artifact identity is the complete tuple `(artifact_id, digest, version)`.
The opaque artifact ID preserves the logical owner namespace, the lowercase
SHA-256 digest binds the bytes, and the version prevents an older model or
bundle generation from being treated as the current one. A matching owner
reference must carry all three values.

## Decision contract

`plan_artifact_gc` evaluates entries in deterministic identity order and records
the candidate, reference, and tombstone snapshot revisions in the result. Its
rules are ordered for safety:

1. Contradictory candidate generations defer with `candidate_identity_conflict`.
2. An incomplete or over-limit reference page defers with
   `reference_scan_incomplete`.
3. An incomplete or over-limit tombstone page defers with
   `tombstone_scan_incomplete`.
4. A live owner reference retains the exact generation. Unknown owner state,
   expired live leases, and same-ID digest/version mismatches defer with their
   explicit reasons. Job and deployment pins remain distinguishable.
5. A same-ID tombstone with another digest or version defers with
   `tombstone_identity_mismatch`; conflicting markers defer with
   `tombstone_conflict`.
6. A matching retention tombstone retains the entry. A matching deletion
   tombstone reasserts deletion using that same marker, preventing a stale
   byte from being resurrected.
7. A future retention deadline or minimum-idle window retains the entry.
8. Only an entry with complete bounded reference/tombstone evidence and no
   matching owner may receive an `eligible` deletion decision. The plan emits a
   deletion tombstone with the exact identity and cache revision.

The policy caps one pass at 256 candidates, 1,024 owner references, and 1,024
tombstones by default. Hosts may lower or raise these values only within the
module's bounded limits. A candidate page may continue with `next_cursor`; the
plan is marked incomplete while more candidates remain. Reference and
tombstone pages must be complete before any entry in the pass can be deleted.

## Adapter apply protocol

A storage adapter should treat the plan as a proposal tied to its three source
revisions. Before applying a delete, it must confirm those revisions still
match, persist the emitted deletion tombstone durably, and delete only the
exact digest/version entry. Retrying the same plan must be idempotent because
an existing matching deletion tombstone is reasserted. A retention tombstone
is a durable hold and remains effective until an explicitly authorized
lifecycle operation removes it.

The adapter must reject a plan after any source revision changes and obtain new
bounded pages. It must never turn `defer` into deletion, infer liveness from a
missing reference, treat a digest-only match as a logical owner match, or
construct a path from model-provided metadata.

## Guarantees and limits

This slice proves deterministic, bounded, fail-closed planning and explicit
identity/tombstone semantics. It does not implement a store, tombstone
persistence, byte deletion, cross-node replication, automatic failover,
reference discovery, or a distributed lease service. A complete page is an
adapter assertion backed by its own durable snapshot; the domain module cannot
prove that an external registry supplied truthful data. Applying a plan
requires an adapter-side revision check immediately before the side effect.

## Verification

```text
python -m pytest -q tests/test_artifact_retention.py
python -m compileall -q sonder_runtime/domain/artifact_retention.py
python scripts/check_architecture.py
python scripts/generate_documentation_catalogs.py --check
python scripts/check_doc_links.py
python scripts/check_requirement_evidence.py
git diff --check
```
