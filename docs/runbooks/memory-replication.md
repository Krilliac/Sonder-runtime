# Bounded memory replication

Sonder's authoritative memory journal can be copied to explicitly configured
SQLite sinks with `MemoryReplicationCoordinator`.  The coordinator is a
small, provider neutral application boundary for a single bounded transfer
page; it does not discover peers or change cluster ownership.

```python
from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
    SQLiteMemoryReplicationJournal,
)
from sonder_runtime.application.memory.replication import (
    MemoryReplicationCoordinator,
    SQLiteMemoryReplicationSink,
)

source = SQLiteMemoryReplicationJournal("source.sqlite", source_id="node-a")
replica = SQLiteMemoryReplicationJournal("replica.sqlite", source_id="node-b")
coordinator = MemoryReplicationCoordinator(
    source,
    (SQLiteMemoryReplicationSink("node-b", replica),),
    minimum_data_replicas=2,
)
outcome = coordinator.replicate(after_sequence=0)
```

For a single-host profile, pass an empty sink tuple and
`minimum_data_replicas=1`; the source remains authoritative and the outcome
does not imply a second durable copy.

`minimum_data_replicas` includes the authoritative source.  A non-empty page
is `replicated` only when that many receipts validate all of the following:

* replica identity is the configured sink identity;
* source identity, source epoch, and next sequence match the page;
* the receipt carries the page's SHA-256 digest; and
* the sink reports durable persistence.

The result records durable and failed identities, stable failure reasons, the
page digest, and the number of newly inserted records.  Replaying a page is
safe because the SQLite journal is idempotent; a replay receipt can therefore
have `inserted_records=0`.  An empty export returns `empty` and never counts
as a peer acknowledgement.

## Explicit HTTPS peer transport

The same coordinator can use the authenticated `HttpsMemoryReplicationSink`
for a configured peer.  A remote origin must use HTTPS; plain HTTP is accepted
only for loopback development/testing.  The sink sends one canonical batch to
`POST /v1/memory/replication/batches`, rejects redirects and oversized bodies,
and accepts a receipt only when its self-authenticating digest, source, epoch,
cursor, replica identity, and durable flag match the batch.

```python
from sonder_runtime.adapters.memory_replication import HttpsMemoryReplicationSink

remote = HttpsMemoryReplicationSink(
    identity="node-b",
    origin="https://node-b.example:8443",
    api_key="a-private-peer-key",
)
coordinator = MemoryReplicationCoordinator(
    source,
    (remote,),
    minimum_data_replicas=2,
)
```

The receiving host constructs `MemoryReplicationReceiver` with its durable
SQLite sink, the same peer key, and an explicit tuple of accepted source IDs,
then injects it with `configure_memory_replication_receiver`.  The HTTP route
is disabled until that injection; it accepts no browser `Origin`, account
fields, or source identity supplied outside the signed batch.  A receiver
failure is returned as unavailable so the coordinator records `pending` rather
than claiming a durable copy.

This transport provides authenticated, digest-bound delivery and idempotent
replay only.  It does not elect an owner, provide quorum, fence processes,
replicate control state, or claim automatic takeover/failback or high
availability.  Those guarantees require a separately reviewed replication and
consensus provider around these receipts.
