# Takeover readiness runbook

Use this runbook when an operator or an external provider is evaluating a
possible owner transition. The procedure is read-only until a separately
authorized provider accepts the resulting decision.

1. Construct a `TakeoverRequest` with the exact cluster, resource, previous
   owner, previous epoch, replacement owner, and configured topology profile.
2. Obtain and independently verify the old-owner fencing receipt. A lease
   timeout, a disconnected node, a preferred-primary label, or a user request
   is not a fencing receipt.
3. Obtain a replication receipt for the same resource and previous epoch. It
   must state that the state is acknowledged and durable and list at least two
   distinct durable holders, including the previous owner's source copy.
4. Obtain a quorum decision from an authority and witness set that are
   independent of both owners and all data replicas. A data replica cannot
   count as a witness.
5. Call `evaluate_takeover_readiness` with the three immutable evidence
   objects. Persist the profile, decision, and reason codes as deployment
   evidence. Do not persist payloads or credentials in this policy layer.
6. Proceed to an external takeover provider only when `decision.ready` is
   true. The pure gate does not perform the promotion.

## Expected blocked states

| Reason | Meaning and action |
| --- | --- |
| `old_owner_fence_missing` or `old_owner_fence_unconfirmed` | The former owner is not proven fenced. Stop and obtain a verified fencing receipt. |
| `replication_evidence_missing`, `replication_not_acknowledged`, or `replication_not_durable` | No safe durable copy has been proven. Keep work paused and repair the replication provider. |
| `replication_quorum_not_reached` or `replication_source_missing` | The evidence does not cover the minimum two holders or the old owner's source. Do not infer a copy from reachability. |
| `quorum_evidence_missing`, `quorum_denied`, or `quorum_not_reached` | No independent witness decision has authorized the transition. Do not promote. |
| `*_scope_mismatch` | Evidence belongs to a different cluster, resource, owner, or epoch. Discard it and investigate stale state. |
| `quorum_not_independent` or `witness_not_independent` | A purported authority or witness overlaps an owner or data replica. Obtain an independent decision. |

For `single-host`, local SQLite durability does not satisfy the replication
gate. For `pooled-pair`, the two configured data nodes do not satisfy the
independent witness gate. Existing deployment configuration therefore remains
fenced against automatic takeover until real provider integrations supply all
three evidence classes.
