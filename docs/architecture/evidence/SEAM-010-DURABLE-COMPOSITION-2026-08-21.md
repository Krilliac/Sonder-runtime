# SEAM-010 durable job/workflow composition — 2026-08-21

This bounded slice connects the existing typed `JobRegistryService` and
`ResumableWorkflowEngine` ports to the same SQLite database used by the
application's durable job registry.

## Composition

- `Application.workflow_engine()` is lazy and cached.
- The engine reuses the cached `Application.job_service()` and therefore the
  same `SQLiteDurableJobRegistry` instance.
- `SQLiteWorkflowCheckpointRepository` owns one bounded table in that same
  database and implements only the typed checkpoint port.
- Workflow start publishes the job before its initial checkpoint. A replay of
  an already complete pair returns the durable job; a job without a checkpoint
  fails closed rather than pretending restart truth exists.

## Invariants verified

- Dependency ordering: the job exists before checkpoint publication.
- Cancellation: terminal cancellation is visible through the durable job
  record and prevents a later workflow claim/resume.
- Idempotency: repeated start of the same durable identity returns the same
  job/checkpoint pair; repeated cancellation returns the terminal record.
- Restart truth: a fresh application graph reads the checkpoint written by the
  previous graph and resumes from its monotonic cursor.
- Checkpoint writes use SQLite transaction-scoped compare-and-set on the prior
  sequence and reject gaps or stale writers.

## Focused evidence

```text
python -m pytest -q tests/test_seam010_durable_composition.py \
  tests/test_wp3_seam010_jobs.py tests/test_sqlite_job_registry_port.py
```

The focused suite passed with 14 tests. Repository-wide commit/push is outside
this slice and remains subject to the existing workspace `.git` ACL/network
restrictions.
