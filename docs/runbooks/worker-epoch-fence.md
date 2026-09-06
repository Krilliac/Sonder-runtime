# Worker owner epoch effect fence runbook

Use this boundary when an adapter is about to let a worker mutate a job,
artifact, workspace, provider, or other owned resource. The policy refuses a
mutation unless the worker presents an observation that names the same scope,
owner, epoch, and lease-token digest as the request.

## Adapter sequence

1. Obtain the authenticated current owner record from the adapter that owns
   the lease or owner journal. Do not infer ownership from a host name, PID,
   endpoint, preferred-primary label, or a reachability timeout.
2. Hash the lease token with SHA-256 and construct an
   `OwnerEpochObservation`. Keep the raw token inside the authenticated
   provider boundary.
3. Construct a `WorkerEffectRequest` for the exact operation and effect class.
   Use `read` for inspection only; use `write`, `execute`, `delete`, or
   `control` for any mutation or external side effect.
4. Call `evaluate_worker_effect(request, observation)` at the effect
   authority boundary. Refuse the operation and preserve the reason code when
   `decision.allowed` is false.
5. For an allowed mutation, continue through the provider's own conditional
   write/effect fence. Refresh the observation for a later effect checkpoint;
   do not reuse a decision as a lease or as a transaction lock.

Example adapter pseudocode:

```python
observation = OwnerEpochObservation(
    cluster_id=lease.cluster_id,
    resource_kind=lease.resource_kind,
    resource_id=lease.resource_id,
    owner_id=lease.owner_id,
    owner_epoch=lease.epoch,
    lease_token_digest=sha256(lease.token.encode("utf-8")).hexdigest(),
    active=lease.expires_at > now,
)
decision = evaluate_worker_effect(request, observation)
if not decision.allowed:
    return {"status": "blocked", **decision.as_dict()}
return provider.conditional_effect(request)
```

The pseudocode is illustrative. The provider must define the authenticated
lease representation, expiry clock, token encoding, and conditional effect;
this domain helper does not choose them.

## Reason handling

| Reason | Operator or adapter action |
| --- | --- |
| `owner_evidence_missing` | Pause the mutation and obtain a fresh authenticated observation. |
| `owner_inactive` | Keep the operation paused; reacquire authority through the owner boundary. |
| `ownership_scope_mismatch` | Reject the evidence and inspect cluster/resource identity. |
| `owner_mismatch` | Reject the stale worker and do not retry the mutation under its identity. |
| `stale_owner_epoch` / `owner_epoch_mismatch` | Refresh authority; never lower or guess an epoch. |
| `stale_owner_token` | Refresh the lease through the provider; do not expose or copy the raw token. |
| `read_unfenced` | Permit the read, while preserving that it carries no mutation authority. |

## Supported profiles and limits

This helper is safe to import in a single-host process or in a two-node pool,
but it does not make either profile highly available. It provides no automatic
takeover, witness, quorum, acknowledged replication, process fencing,
database failover, or distributed transaction. A two-node deployment must
continue to follow the fenced capabilities in the
[deployment topology runbook](deployment-topology.md). A provider may use
this contract as one prerequisite for a future takeover protocol only after
the old owner, durable state, and independent authority have separately been
verified.

The decision is only a point-in-time check. An adapter that needs protection
across a multi-step operation must combine it with a provider-owned
conditional mutation or transaction and recheck at each effect boundary.
