# REMAINING-JOB-002-004 — Durable job registry and process containment seam

## Scope

`DurableJobRegistry` is the single parent-aware application lifecycle for
generic jobs, workflows, and execution-world jobs. It provides start, list,
poll, bounded stream, cancel, and terminal collect operations over immutable
`JobRecord` values. Child creation requires an existing parent and cancellation
propagates through the complete descendant tree.

## Recovery

`reconcile` translates registry records into the existing bounded startup
reconciliation contract. It returns a `DrainPlan` and only applies the
fail-closed `interrupted` transition for records that need marking. It does not
claim that an active process is healthy, and it does not resume work silently.

## Process-tree containment

`ProcessTreeCleanupContract` is the platform adapter boundary. The registry
creates a bounded `ProcessTreeCleanupRequest` from a reconciliation intent;
the adapter must return a `ProcessTreeCleanupReceipt` with explicit counts and
completion truth. No application-layer test or contract claims that an
in-memory record terminates an OS process tree.

## Evidence

- `tests/test_remaining_job_registry.py` covers the parent-linked lifecycle,
  bounded output, descendant cancellation, terminal collection, restart
  reconciliation, cleanup request conversion, and truthful cleanup receipts.
- Formal master-spec checkboxes were intentionally left unchanged.
