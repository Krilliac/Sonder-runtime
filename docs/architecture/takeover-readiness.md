# Takeover readiness contract

`sonder_runtime.domain.takeover_readiness` is a pure evidence gate. It
describes whether an external takeover provider has supplied the evidence
needed to continue an owner transition. It never contacts a node, stops a
process, elects an owner, replicates data, or changes a lease.

The gate returns `ready=true` only when all three inputs match the requested
cluster, resource, previous owner, and owner epoch:

1. `OldOwnerFenceEvidence` contains a non-empty receipt from an authority that
   explicitly confirmed the previous owner was fenced. The authority must be
   distinct from both the previous and replacement owner.
2. `DurableStateReplicationEvidence` says the state was acknowledged and
   durably stored on at least two distinct holders by default. Its replica set
   must include the previous owner's source copy. Larger deployments can raise
   the bounded `minimum_replicas` requirement.
3. `IndependentQuorumDecision` is granted by an authority with enough witness
   votes. The authority and witness IDs must be independent from both owners
   and every data replica.

Evidence is immutable and bounded. The constructors validate identity, digest,
epoch, receipt, vote, and member shape. They do not verify cryptographic
signatures or perform the provider operation. The provider must complete those
checks before handing evidence to `evaluate_takeover_readiness`.

```python
from sonder_runtime.domain.takeover_readiness import (
    DurableStateReplicationEvidence,
    IndependentQuorumDecision,
    OldOwnerFenceEvidence,
    TakeoverRequest,
    TakeoverTopology,
    evaluate_takeover_readiness,
)

request = TakeoverRequest(
    cluster_id="cluster-a",
    resource_kind="session",
    resource_id="session-1",
    previous_owner_id="node-old",
    previous_epoch=7,
    new_owner_id="node-new",
    topology=TakeoverTopology.TWO_NODE,
)
decision = evaluate_takeover_readiness(
    request,
    fence=verified_fence_receipt,
    replication=verified_replication_receipt,
    quorum=verified_quorum_decision,
)
if decision.ready:
    # Pass the decision to the separately-owned provider boundary.
    request_external_takeover()
else:
    record_blocked_reasons(decision.reason_codes)
```

The `ready` result is an input to a provider boundary. It is not permission
for a caller to promote a node directly. A missing, stale, unconfirmed, or
conflicting input returns `blocked` with stable reason codes.

## Profile limits

The profile is visible in every result so a UI or operator report cannot hide
the deployment shape:

| Profile | Contract limitation |
| --- | --- |
| `single-host` | Local SQLite is local durability only. A standalone PC cannot claim takeover safety without an external replicated state provider, fencing authority, and witness. |
| `pooled-pair` | Two data nodes do not form an independent quorum by themselves. A third witness or equivalent independent quorum service is required to prevent split-brain. |
| `multi-node` | Membership and resource pooling do not implement elections, fencing transport, replication transport, or failback. Those capabilities remain external. |

These limits supplement the deployment topology status in
[`deployment-topology.md`](../runbooks/deployment-topology.md), which continues
to report automatic takeover and failback as unavailable for the current
runtime profiles. This contract records the evidence boundary needed for a
future provider; it does not enable those profiles.
