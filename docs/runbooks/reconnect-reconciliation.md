# Client reconnect and worker receipt reconciliation runbook

Use this contract when a desktop, mobile, SDK, or other trusted client loses
its connection while a worker-owned operation may still be running. The
policy is an evaluation seam for an authenticated adapter. It does not perform
network discovery or resume work itself.

## Reconnect sequence

1. Keep the client's stable `client_id`, optional `session_id`, and last
   `AuthorityLease` in the client or its protected session store. Do not use a
   host name, process ID, IP address, or endpoint URL as the client identity.
2. Ask the authenticated discovery adapter for one bounded
   `DiscoverySnapshot`. Include the current cluster ID, owner epoch, lease ID,
   lease expiry, snapshot revision, and endpoint observations.
3. Call `ReconnectReconciliationPolicy.discover(request, snapshot, now=...)`.
   Use the selected endpoint only for a `connected` result. Preserve the
   reason code and wait for fresh authority evidence on `paused`; retry the
   discovery path on `unavailable`; repair the client or cluster identity on
   `rejected`.
4. If an operation may have been submitted before the disconnect, query the
   worker's durable receipt through the authenticated worker adapter. The
   receipt must carry the same stable `client_id`, and every returned snapshot
   for the exact operation goes to
   `reconcile(request, receipts, current_authority=..., now=...)`.
5. Apply `resume` only through the worker's existing idempotency and owner
   fencing boundary. Apply `replay` by returning the already terminal receipt.
   Keep `paused`, `unavailable`, and `rejected` visible to the client; do not
   turn any of them into a fresh submission.

## Reason handling

| Reason | Meaning | Required adapter response |
| --- | --- | --- |
| `authority_stale` / `authority_ahead` | The client's last binding and the observed binding disagree on epoch. | Pause and obtain a fresh authenticated authority exchange. No automatic promotion. |
| `authority_ambiguous` / `lease_mismatch` | The same epoch names different owners or leases. | Reject the evidence and investigate the authority source. |
| `authority_expired` | The observed lease cannot authorize a reconnect at the supplied time. | Pause until a new lease is issued and observed. |
| `member_unavailable` / `protocol_mismatch` | No reachable endpoint can satisfy the bounded request. | Report unavailable and retry with bounded backoff. |
| `receipt_stale` / `receipt_ahead` | Worker evidence is outside the current owner epoch or client cursor. | Keep work paused; do not replay or resubmit. |
| `client_mismatch` | Receipt evidence belongs to a different stable client identity. | Reject it and require an authenticated lookup for the requesting client. |
| `idempotency_conflict` / `receipt_conflict` | One operation identity maps to incompatible request or worker evidence. | Fail closed and require operator or provider reconciliation. |
| `receipt_not_found` | No receipt matched the exact operation and idempotency key. | Report unavailable; do not infer that the operation did not run. |
| `worker_paused` / `worker_interrupted` | The worker has explicit non-terminal stopped state. | Keep paused and use the worker's explicit resume control if supported. |

## Bounds and guarantees

- Discovery snapshots contain at most 256 members; a policy may lower this
  bound. Endpoint and node identities are unique and protocol versions are
  bounded, sorted, and explicit.
- Receipt reconciliation consumes at most 1,024 receipt snapshots by default;
  a policy may lower this bound. Repeated identical snapshots are
  deduplicated, while distinct remote-job identities for one idempotency key
  are rejected.
- Receipt revisions and output watermarks are non-negative bounded integers;
  revisions are positive and watermarks cannot move backwards across retained
  revisions.
- SHA-256 request digests, owner epochs, lease IDs, operation IDs, and worker
  identities are part of the exact receipt binding. Receipt evidence is not a
  permission, a lease, a quorum vote, or a failover proof.
- No process, database, network, or installed runtime is changed by this
  domain policy. Live transport and persistence integration remains a separate
  adapter task.

For current two-PC behavior and its explicit takeover limitations, see the
[deployment topology runbook](deployment-topology.md). For whole-job worker
idempotency and artifact receipts, see the [compute fabric
runbook](compute-fabric.md).
