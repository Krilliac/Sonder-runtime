# External control-state provider transport

`sonder_runtime.adapters.cluster.HttpsControlStateProvider` is the transport
adapter for a separately operated replication, quorum, and fencing service. It
does not elect an owner, merge local SQLite files, or promote a node by itself.
The runtime continues to keep the single-PC and pooled-pair profiles disabled
for automatic takeover until an operator composes this adapter with a provider
that supplies the required evidence.

## Provider contract

The provider origin must be an HTTPS origin without credentials, paths, queries,
or fragments. Plain HTTP is accepted only for an explicitly enabled loopback
test endpoint. Every request carries the configured bearer key and provider
identity; redirects, oversized bodies, unexpected status codes, malformed JSON,
and free-form extension fields are refused.

The fixed endpoints are:

```text
POST /v1/control-state/events
GET  /v1/control-state/events?cluster_id=<id>&after_sequence=<n>&limit=<n>
POST /v1/control-state/fence
```

An append response is exactly:

```json
{
  "object": "replication_acknowledgement",
  "acknowledgement": {
    "event_id": "...",
    "cluster_id": "...",
    "owner_epoch": 1,
    "sequence": 1,
    "provider_id": "...",
    "protocol_version": 1,
    "data_replica_ids": ["node-a", "node-b"],
    "witness_ids": ["witness-a"],
    "durable": true
  }
}
```

The client binds the acknowledgement to the exact event ID, cluster, epoch,
sequence, provider, and protocol. A durable acknowledgement must still be
checked with `validate_replication_acknowledgement`; witnesses never count as
data replicas. The read endpoint returns an exact `control_state_events`
envelope with at most 128 strictly increasing events for one cluster.

A fence response is exactly:

```json
{
  "object": "fence_receipt",
  "receipt": {
    "receipt_id": "...",
    "cluster_id": "...",
    "resource_kind": "job",
    "resource_id": "job-1",
    "previous_owner_id": "node-a",
    "previous_owner_epoch": 1,
    "provider_id": "...",
    "protocol_version": 1,
    "partition_state": "safe",
    "external": true,
    "accepted": true
  }
}
```

The receipt is returned even when `accepted` is false so the caller can record a
bounded denial. The caller must pass both receipts through the pure takeover
gate before asking a separate ownership adapter to advance an epoch. A timeout,
successful TCP connection, or capability declaration is not a fence receipt.

## Composition and limits

Construct the client with a provider capability declaration that names the
provider's data replicas and independent witnesses. That declaration is an
admission hint only; it is not proof that the remote service is durable or
independent. The external service must enforce its own authentication,
replay/nonce policy, replica durability, quorum, and old-owner fencing, and it
must return receipts bound to the requested scope.

The current runtime does not automatically select this provider from a model
request or a UI command. Automatic takeover and failback remain unavailable in
the default profiles until the provider is composed into the deployment
control plane and live failure rehearsal proves the complete path.

`sonder_runtime.application.control_state.ExternalControlStateCoordinator`
is the explicit application seam for that composition. It validates the
provider's append/read/fence receipts, evaluates the existing fail-closed
takeover gate, and returns a bounded `ControlStateTakeoverAttempt`. It never
promotes an owner, retries an ambiguous write, or creates a provider from
configuration; a separately owned durable authority must consume an allowed
attempt before changing an epoch.

## Verification

The transport and DTO boundary are covered by
`tests/test_http_control_state_provider.py`, including TLS/origin policy,
redirect and response bounds, exact event/receipt binding, ordered reads,
non-durable acknowledgements, and denial handling. The tests use an injected
opener and never contact a real node or modify installed state.
