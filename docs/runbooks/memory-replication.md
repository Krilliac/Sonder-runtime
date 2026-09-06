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

The current sink is intentionally local and explicit.  There is no network
transport, owner election, quorum service, automatic takeover/failback,
cross-process fencing, or claim of high availability in this contract.  A
deployment that needs those guarantees must add a separately reviewed
transport and control-state provider around these receipts.
