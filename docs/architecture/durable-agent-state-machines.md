# Durable agent state machines

Sonder's durable job registry is the shared lifecycle seam for agent work,
workflows, and execution jobs. The SQLite adapter provides the concurrency
authority; application services validate requests and preserve compatibility
with adapters that implement the earlier, smaller port.

## Lease and retry contract

- Every successful claim increments the persisted `attempt` and rotates an
  opaque `claim_token`. The token is a fencing value, not a credential.
- New workers should pass the returned token to heartbeat, checkpoint, finish,
  and receipt-commit calls. A stale token cannot extend or commit a newer lease,
  even when a worker ID is reused after restart.
- `max_attempts` defaults to three, is capped at 100, and is persisted with the job. An explicit
  retry moves only `failed` or `interrupted` work back to `pending`, uses a
  revision compare-and-set when supplied, and never resets the attempt count.
- Legacy callers may omit the fencing token. Their worker-ID/lease behavior is
  preserved, while upgraded workflow callers receive the claim in
  `WorkflowResume.claim` and can use the stronger contract.

## Completion receipts

`finish_once` commits the terminal record and a `durable_job_receipt` in one
SQLite transaction. Receipts are unique per `(job_id, attempt)` and receipt keys
are globally unique. Replaying the same key and payload returns the original
receipt; changing the key, status, or payload fails without mutating the job.

This is an exactly-once *commit* guarantee, not a claim that arbitrary external
side effects execute exactly once. Side-effecting tools still need their own
idempotency key or transactional boundary.

## Cancellation and reconciliation bounds

- Descendant cancellation traverses in stable breadth-first order and validates
  `max_descendants` before changing any row. The cancellation transaction either
  clears every affected lease/token and marks every non-terminal descendant, or
  rolls back entirely.
- `reconcile_stale` processes at most `max_records` expired leases and returns a
  `JobReconciliationReport` with scanned IDs, interrupted IDs, and a `truncated`
  flag. Operators should call it again while `truncated` is true. The older
  `reconcile` method remains as a count-only compatibility projection.

## Dependency ordering and fanout

`TaskLedger` rejects missing edges, self-edges, duplicate edges, and dependency
cycles. `ready_items()` returns only tasks whose dependencies are successful and
caps the dispatch batch with `max_fanout`. `blocked_dependencies()` explains
which prerequisites prevent dispatch, while `dependency_batches()` provides a
stable bounded topological projection for diagnostics and planning.

## Recovery and rollback

Schema initialization adopts existing job databases in place by adding nullable
or defaulted columns and creating the receipt table/index. No destructive data
migration is required. Rolling back the runtime leaves the added SQLite columns
and receipt table inert; older code continues reading its original columns.
