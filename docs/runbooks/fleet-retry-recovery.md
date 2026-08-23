# Fleet retry and recovery

How an interrupted, failed, or cancelled master fleet is replayed safely.
Everything here is enforced by `master_orchestrator` + the durable
`fleet_store` ledger; the runbook explains what an operator sees and which
knobs exist.

## Replaying a persisted master

1. Find the candidate: `master_status()` lists persisted masters. Lineage is
   shown per row — `retry of: <source-id>` on a replay, `retried by: <id>`
   on a source that was successfully replayed, and
   `retry claimed: dispatch in progress` while a dispatch is being fenced.
2. Retry explicitly: `master_retry('<id or unique prefix>')`. Only
   `interrupted`, `failed`, `cancelled`, or `task_drift` masters are
   accepted; the persisted task text is verified against its immutable
   digest before any model call.
3. Duplicate protection: retry dispatch is idempotent. A second
   `master_retry` for the same source — concurrent or repeated — is refused
   while a retry is in flight (`ERROR: ... retry in flight`). The fence is a
   short-lived pending claim on the source row (bridges only the dispatch
   window, TTL ~2 minutes) plus the durable retry master row itself. An
   unsuccessful retry clears the claim in the same transaction that makes
   the retry terminal, so the next attempt is possible immediately.
4. A successful retry marks the source `retried` / `retried_by=<new id>`;
   further retries of that source are refused.

## Worker failure classification and transient retries

Delegated worker failures are labelled with the same closed vocabulary as
the fanout receipt store (`[timeout]`, `[unavailable]`, `[throttled]`,
`[transport]`, `[request_rejected]`, `[unknown]`); the label is the prefix
of the child's persisted error and appears in `master_status()`.

- Only `timeout`/`unavailable`/`throttled`/`transport` are considered
  transient; those earn one bounded in-run retry of the model call.
  Everything else fails permanently on the first attempt.
- Cancellation is re-checked before every extra attempt: a cancel arriving
  during a failed call is honored instead of starting another request.
- `SONDER_FLEET_TRANSIENT_RETRIES` bounds the extra attempts (default 1,
  clamped 0–3; `0` disables in-run retries). The interactive inline lane
  never retries automatically.

## Partial fanout

If queueing the fleet's children fails midway (ledger contention, disk),
the already-queued children are cancelled in the same pass and the master
finishes `failed` with
`fleet startup failed after queueing N of M delegated agents`. No child is
left `queued` with no worker attached.

## Invariants (violations are bugs, file them)

- One retry dispatch per source at a time; concurrent `master_retry` calls
  have a single winner.
- A pending retry claim never survives an unsuccessful retry, and never
  overwrites a recorded successful `retried_by`.
- A cancelled worker never issues another model call from the transient
  retry path.
- Fleet startup either queues every child or cancels the ones it queued.
