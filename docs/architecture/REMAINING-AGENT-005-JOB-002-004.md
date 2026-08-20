# REMAINING-AGENT-005-JOB-002-004 — Durable lineage and job integration

`SQLiteDurableJobRegistry` is the durable implementation of the existing
parent-linked job lifecycle. It persists job identity, operation linkage,
parent/child relationships, status revisions, process IDs, bounded output,
and recovery state. Re-opening the adapter reconstructs the same operator
queryable records; it does not depend on process memory.

`DurableLineageQuery` joins that job projection with the existing SQLite
durable child-session repository. It exposes bounded, read-only descendants
and operator filters for root, kind, and status. Prompts, output, and
arbitrary metadata are intentionally absent from the projection.

Recovery uses the existing startup reconciliation classifier. Orphaned active
jobs produce bounded `ProcessTreeCleanupRequest` values for an injected
`ProcessTreeCleanupContract`. The adapter never kills a process itself and
only marks the job interrupted after a truthful, complete cleanup receipt.
Incomplete cleanup leaves the durable job active for operator review.

Evidence: `tests/test_remaining_agent_005_job_integration.py` covers restart
persistence, joined parent/child discovery, bounded operator exposure,
orphan recovery, incomplete cleanup truthfulness, and process identity bounds.
The formal master checklist and conservative audit were intentionally left
unchanged.
