# REMAINING-AGENT-006 — Durable continuable child sessions

## Scope

This slice closes the AGENT-006 durability gap between the WP5 continuable
subagent service and the repository-backed session/job contracts. It adds a
transactional SQLite repository and a service boundary that keeps worker
threads ephemeral while persisting the child request, parent lineage,
monotonic checkpoint, cancellation intent, recovery state, and terminal
result.

## Contract

- `SQLiteDurableContinuationRepository` is the persistence adapter. Each
  checkpoint is committed with a transaction-scoped compare-and-set on the
  prior checkpoint sequence and repository revision.
- `ChildSessionLineage` records the explicit parent plus the inherited
  ancestor chain. Cyclic lineage is rejected before publication.
- `DurableContinuationService` may be recreated against the same database.
  `resume` starts from the last committed checkpoint; it does not replay the
  runner's already-completed prefix.
- Cancellation is an append-once durable intent. The first reason wins and a
  fresh service instance observes it before accepting a resume.
- `recover_after_restart` converts an orphaned running record into a retryable
  interrupted result. It never claims that an operating-system process was
  terminated.

## Evidence

Focused coverage is in `tests/test_remaining_durable_subagents.py`:

1. SQLite round-trip of lineage and checkpoint plus stale-writer rejection.
2. Resume from a durable checkpoint through a new service instance.
3. Parent/child lineage persistence and cycle rejection.
4. Durable cancellation and first-reason-wins semantics.
5. Restart recovery of an orphaned running child.

The formal master checklist is intentionally unchanged; this evidence is an
implementation slice, not permission to mark AGENT-006 complete globally.
