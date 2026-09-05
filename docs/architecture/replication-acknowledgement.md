# Replication acknowledgement contract

**Status:** implemented policy boundary; provider integration remains future work.

Sonder can commit an event to its local SQLite database without having a copy
on another machine. The domain policy keeps that local fact separate from a
replication acknowledgement. It is pure Python and has no database, network,
process, consensus, takeover, or fencing side effects.

## Modes

| Mode | Required remote data copies | Meaning |
| --- | ---: | --- |
| `local_sqlite` | 0 | The event may be reported `local_only` after a local durable commit. This is never a replication acknowledgement. |
| `pooled_pair` | 1 | A two-PC pool can acknowledge one authorized remote data copy. A witness is unnecessary for this data-copy check. |
| `replicated_data_quorum` | 2 or more | The configured number of distinct authorized remote data replicas must acknowledge the event. |

The required count applies to **remote data copies**. The source/local SQLite
commit does not satisfy it. A policy with an empty authorized replica set
returns `unavailable`; it does not infer membership from a preferred primary,
worker reachability, or a configured peer URL.

## Evidence binding

`ControlEvent` carries the immutable `cluster_id`, `event_id`,
`source_node_id`, `owner_id`, `owner_epoch`, `lease_id`, and `event_digest`.
`ReplicaAcknowledgement` repeats every field that affects authority. A receipt
counts only when all of these match, its replica is in
`authorized_data_replica_ids`, its role is `data`, `authorized`, `reachable`,
and `durable` are true, and its ID differs from both the source and local
node. The policy counts one copy per replica ID. Conflicting receipts from one
replica remove that replica from the count and produce
`conflicting_replica_evidence`.

An `arbitration_witness` receipt is never a data copy, even when it is
reachable, durable, and authorized as a witness. Witness IDs and data replica
IDs must be disjoint. The policy can therefore describe a future witness
without making the witness a substitute for replicated state.

`ReplicationDecision.acknowledged` and `.replicated` are true only when the
remote data-copy requirement is met. `.takeover_safe` is always false:
replication evidence does not fence an old owner or elect a new one.

## Conservative states

- `acknowledged`: local commit and every configured remote data-copy condition
  passed. The event can be handed to a provider that separately performs
  fencing/consensus checks.
- `local_only`: local SQLite is durable, but no replicated safety claim exists.
- `paused`: progress is intentionally held for a partition, unreachable
  replica, missing local commit, or an incomplete quorum.
- `unavailable`: no usable replication evidence is present or the data replica
  set is not configured.

Use `reason_codes` and `as_dict()` for operator-visible status. Stable reasons
include `local_sqlite_only`, `data_replica_evidence_missing`,
`data_quorum_not_reached`, `partition_prevents_ack`,
`owner_epoch_mismatch`, `lease_mismatch`, and `event_digest_mismatch`.

## Boundary example

```python
from sonder_runtime.domain.replication_ack import (
    ControlEvent,
    ReplicaAcknowledgement,
    ReplicationAcknowledgementPolicy,
    ReplicationMode,
)

event = ControlEvent(
    cluster_id="cluster-a",
    event_id="event-1",
    source_node_id="node-1",
    owner_id="owner-a",
    owner_epoch=7,
    lease_id="lease-7",
    event_digest="a" * 64,
)
policy = ReplicationAcknowledgementPolicy(
    mode=ReplicationMode.POOLED_PAIR,
    local_node_id="node-1",
    authorized_data_replica_ids=frozenset({"node-2"}),
)
receipt = ReplicaAcknowledgement.for_event(event, "node-2")
decision = policy.evaluate(event, (receipt,), local_durable=True)
assert decision.acknowledged
```

The receipt's `durable=True` is provider evidence, not a write performed by
this module. An adapter must only construct that evidence after its own
bounded, authorized durable operation. This contract does not make a live
network replication path or a consensus provider available.
