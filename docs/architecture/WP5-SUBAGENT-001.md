# WP5 SUBAGENT-001 — durable continuable subagents

`sonder_runtime/application/subagents/continuable.py` adds the application
service for child work that can survive an interrupted worker and continue from
its last durable checkpoint.

The service builds on `application.ports.subagents`: requests retain explicit
parent linkage, budgets remain provider-neutral, snapshots/results use the
existing terminal status taxonomy, and handles remain non-owning. A
`ContinuableSubagentRepository` owns persistence. Its checkpoint write is a
compare-and-set on the prior sequence, so stale workers cannot overwrite newer
state. `InMemoryContinuableSubagentRepository` is only a thread-safe reference
adapter for tests; it is not a production durability claim.

Lifecycle behavior:

- `spawn` creates a child and launches a cooperative runner.
- The runner receives immutable-at-boundary state, a checkpoint writer, and a
  cancellation signal. Checkpoints are monotonic and include a cursor.
- Runner failures and deadline interruptions become retryable durable results;
  the last checkpoint remains available.
- `resume` is explicit and only accepts records marked recoverable.
- `recover` converts orphaned running records into retryable `interrupted`
  failures after a host restart.
- `cancel` is cooperative and first-reason-wins; terminal results are emitted
  exactly once by the worker path.

Focused coverage is in `tests/test_wp5_continuable_subagents.py` and covers
checkpoint/resume, cancellation, restart recovery, and stale-writer rejection.
No formal specification checkbox is changed by this slice.
