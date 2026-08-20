# WP5-JOB-001 — Typed generic jobs

## Scope

This slice provides a provider-neutral generic-job contract above the WP3
durable job port. `GenericJob[T]` carries a typed handler, explicit dependency
ids, and a bounded `RetryPolicy`. `GenericJobExecutor` validates references,
produces stable topological order, and emits immutable `ExecutionRecord` values
for every attempt. A failed dependency produces a `blocked` record and never
executes downstream work.

## Retry and durability boundary

Retry is explicit: the executor records the failure first, then consults an
optional `RetryHook`. The hook may allow another bounded attempt, but cannot
increase `max_attempts` or make a failed dependency successful. Persistence,
leases, cancellation, and restart recovery remain the responsibility of the
WP3 job/workflow adapter; records are the translation seam for that adapter.

## Evidence

- `tests/test_wp5_generic_jobs.py` covers stable dependency ordering and typed
  value passing, per-attempt records and retry hooks, downstream blocking, and
  missing/cyclic dependency rejection.
- Formal master-spec checkboxes were intentionally left unchanged.
