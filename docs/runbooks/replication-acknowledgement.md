# Replication acknowledgement runbook

This runbook describes how to interpret the pure replication policy. It does
not install a replica, configure a network link, promote a node, or alter
Sonder's permissions.

## Select a profile

1. Use `local_sqlite` for one PC. Treat a `local_only` decision as local
   durability only. Do not call it replicated and do not use it as takeover
   proof.
2. Use `pooled_pair` for a two-PC private pool when one remote node is an
   authorized data store. Configure exactly the remote data identity in
   `authorized_data_replica_ids`; a preferred primary remains an operator
   preference.
3. Use `replicated_data_quorum` only when an external provider has enrolled at
   least two distinct remote data replicas and can return receipts bound to the
   same event, owner epoch, and lease. Set `required_data_replicas` to the
   number of remote copies required by that provider.

Keep arbitration witnesses in `arbitration_witness_ids`. They may support a
future arbitration service, but their acknowledgements never satisfy the data
copy count.

## Evaluate a control event

1. Persist the event in the local owner domain and obtain a durable commit
   result.
2. Compute or receive one immutable event digest. `ControlEvent.from_payload`
   computes the canonical `sonder.control-event.v1` SHA-256 envelope digest.
3. Ask the authorized provider to return receipts only after it durably stores
   the exact event. Each receipt must repeat the cluster, event, source,
   owner, epoch, lease, and digest fields.
4. Evaluate with
   `ReplicationAcknowledgementPolicy.evaluate(event, receipts,
   local_durable=True)`. The call performs no I/O.
5. Record the returned state and `reason_codes`. Acknowledge only when
   `decision.acknowledged` is true. Keep all other outcomes paused or
   unavailable according to their reasons.

## Respond to failure states

| State/reason | Operator action |
| --- | --- |
| `local_only` / `local_sqlite_only` | Continue local work if appropriate; preserve the distinction from replication. |
| `unavailable` / `data_replica_not_configured` | Enroll an authorized data replica through a provider, then retry the event. Do not infer membership. |
| `unavailable` / `data_replica_evidence_missing` | Keep the event pending until a verified receipt exists. |
| `paused` / `partition_prevents_ack` or `replica_unreachable` | Repair the partition or wait for a fresh provider receipt. Do not promote or fabricate an acknowledgement. |
| `paused` / `data_quorum_not_reached` | Keep the event pending until the required number of distinct data replicas acknowledges. A witness does not close the gap. |
| any epoch, lease, identity, or digest mismatch | Discard the mismatched receipt and investigate stale or cross-cluster state. Never rewrite the event to fit it. |

`takeover_safe` remains false for every decision. Automatic takeover,
failback, old-owner fencing, live replication transport, and consensus are
separate provider capabilities and are not supplied by this policy.

## Evidence checklist

Before treating a receipt as accepted, verify the policy output contains the
expected event digest and owner epoch, the remote IDs are distinct, and the
count is based only on data roles. Keep the exact policy configuration,
receipt identities, decision state, and reason codes in the deployment evidence
record without storing event payloads or credentials in this policy layer.
