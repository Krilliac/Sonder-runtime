# Worker owner epoch effect fence

`sonder_runtime.domain.worker_epoch_fence` is the narrow contract for deciding
whether one worker operation may perform an effect under an observed owner
lease. It is deliberately pure: the module validates bounded values and
returns an immutable decision. It does not read SQLite, renew a lease, contact
a node, inspect a process, or perform the requested effect.

## Contract

Every mutating request carries a cluster, resource kind, resource ID, worker
owner, positive owner epoch, SHA-256 digest of the lease token, effect kind,
and operation ID. A provider or persistence adapter supplies an observation at
the effect checkpoint. A mutation is allowed only when the observation is
active and its scope, owner, epoch, and token digest all match exactly.

The decision is fail-closed and has a stable reason code:

| Situation | Decision | Reason |
| --- | --- | --- |
| Exact active owner observation | allow | `current_owner` |
| No observation | block | `owner_evidence_missing` |
| Inactive observation | block | `owner_inactive` |
| Different cluster/resource scope | block | `ownership_scope_mismatch` |
| Different owner | block | `owner_mismatch` |
| Observation behind the request | block | `stale_owner_epoch` |
| Observation ahead of the request | block | `owner_epoch_mismatch` |
| Different token digest | block | `stale_owner_token` |
| Read effect | allow | `read_unfenced` |

Read effects remain unfenced so inspection and reconciliation can continue
after a worker loses mutation authority. The status projection omits the token
digest; callers can expose its reason, epochs, and operation ID without
leaking lease material.

## Composition boundary

An adapter can translate a durable owner record into an
`OwnerEpochObservation`, hash the lease token before constructing the value,
and attach a `WorkerEffectRequest` to a job or capacity admission. The result
can be checked immediately before a mutating call and then composed with the
thread-local [effect fence](../../sonder_runtime/adapters/execution/effect_fence.py)
or another provider-owned admission gate. The helper does not replace those
gates and is not wired to a particular persistence schema, so it does not
duplicate owner-journal or replication semantics.

The function is a snapshot check. The adapter owns the safe checkpoint and
must obtain a fresh observation at the authority boundary. A successful
decision does not make a later operation atomic and does not claim protection
against a time-of-check/time-of-use race. Providers remain responsible for
verifying the lease token and for making the actual mutation conditional on
the current owner state.

## Guarantees and limits

- Values and status output are bounded and immutable; raw lease tokens never
  enter this domain contract.
- A stale owner epoch or token cannot receive an allow decision for a
  mutation.
- The module has no network, process, database, filesystem, or model side
  effects.
- It does not implement leader election, quorum, acknowledged replication,
  failover, process fencing, or automatic takeover on a single host or a
  two-node deployment.
- It does not attest hardware or cryptographically verify a token. The
  authenticated owner/lease adapter must provide trustworthy observations.

This slice is therefore a reusable effect-admission contract, not an HA
guarantee. See the [deployment topology runbook](../runbooks/deployment-topology.md)
for the currently supported single-host and pooled-pair profiles.
