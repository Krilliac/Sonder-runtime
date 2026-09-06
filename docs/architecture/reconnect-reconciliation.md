# Client reconnect and worker receipt reconciliation

`sonder_runtime.domain.reconnect_reconciliation` is the provider-neutral
contract for reconnecting a stable client to a private Sonder cluster and
reconciling a worker-owned operation after a transport interruption.

The contract accepts two kinds of adapter-supplied evidence:

- `DiscoverySnapshot` contains a bounded endpoint inventory and the owner
  epoch/lease binding that the adapter observed.
- `WorkerReceipt` contains the stable client identity, exact operation,
  idempotency key, request digest, worker identity, owner binding, state,
  revision, and output watermark returned by a worker.

`ReconnectReconciliationPolicy.discover()` selects a deterministic reachable
endpoint only when the snapshot still carries a live authority binding. A
client's stable `client_id` and optional `session_id` remain separate from an
endpoint identity. A stale epoch, a future epoch, a same-epoch lease conflict,
an expired lease, a cluster mismatch, and an unavailable endpoint produce an
explicit paused, rejected, or unavailable decision. A newer observed epoch
does not promote a node or silently take over a client's session.

`ReconnectReconciliationPolicy.reconcile()` performs an exact idempotency
lookup. It selects the highest monotonic receipt revision for one worker and
  remote-job identity, deduplicates identical snapshots, and checks output
  watermarks for rollback. A receipt for a different stable client cannot be
  used. One idempotency key cannot name two distinct remote jobs. A request
  digest conflict, a stale or future owner binding, a lease mismatch, and a
  same-revision receipt conflict stop the operation. A
non-terminal `pending`, `claimed`, or `running` receipt produces a `resume`
plan; a terminal receipt produces a `replay` plan; paused or interrupted work
stays paused.

The module is pure domain code. It does not open a socket, discover a node,
read SQLite, query a worker, issue a lease, elect an owner, fence a process,
replicate data, or execute a resume. An adapter must obtain authenticated
snapshots and receipts, apply its own authorization and durable-write rules,
and treat `resume` as a plan that still requires the worker's exact
idempotency protocol. Receipt evidence never grants takeover authority.

## Decision vocabulary

| Surface | Positive result | Explicit non-success results |
| --- | --- | --- |
| Discovery | `connected` with a selected endpoint | `unavailable`, `paused`, or `rejected` plus a stable `ReconnectReason` |
| Worker receipt | `resume` for non-terminal work or `replay` for terminal work | `unavailable`, `paused`, or `rejected` plus a stable `ReconnectReason` |

The `as_dict()` projections contain bounded identities, revisions, digests,
states, and reason codes for health or UI surfaces. They do not contain
credentials, prompts, model output, or a reusable permission grant.

This is a prerequisite contract for future transport integration. It does not
claim automatic failover or cross-node durable-state replication; those
capabilities remain governed by the current [deployment topology
contract](../runbooks/deployment-topology.md).
