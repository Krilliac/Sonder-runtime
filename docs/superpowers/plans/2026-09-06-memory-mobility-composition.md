# Memory Mobility Composition Evidence Plan

> **For agentic workers:** This is an evidence and contract plan. Do not
> implement a configuration-only receiver from it. The normal write path must
> first satisfy the contracts in [Required contracts](#required-contracts).

**Goal:** Make a future trusted-node memory mobility feature durable and
truthful, without presenting an empty journal, a receiver-only route, or an
unfenced data copy as live runtime replication.

**Architecture:** The existing project has three useful but disconnected
pieces: a bounded authoritative journal, an authenticated batch receiver, and
a projection into the legacy memory database. A valid production feature must
make the normal write side append an ordered authoritative mutation in the
same durable transaction, then make a configured peer apply and materialize
that mutation before returning its receipt. Configuration and lifecycle
composition belong outside the HTTP adapter.

**Tech Stack:** Python 3, SQLite, stdlib HTTP/HTTPS transport, typed
`SonderConfig`, existing `MemoryReplication*` contracts.

**Spec:** Accepted Sonder Runtime roadmap: durable memory/artifact mobility
across explicitly trusted private nodes, while automatic takeover, consensus,
unbounded scale, and installed-runtime changes remain out of scope.

**Status:** Tasks 1 and 2 are implemented locally for one explicit write set:
a project-scoped `fact` source can be injected into a unit of work and records
its materialized row, source state, and journal mutation in one SQLite
transaction. An explicitly constructed fact-only replica sink now persists a
received page and projects it into the target's normal fact store in one
connection-owned transaction before it returns a receipt. It remains
deliberately uncomposed: no normal runtime root injects either side, and no
configuration, listener, peer service, or retry loop exists. The existing HTTP
adapter appears only in the Task 2 in-process test harness. Ordinary runtime
memory writes therefore remain legacy and are not represented as live
replication.

---

## Evidence at the inspected revision

Inspected base: `a2be46f7ff152ce74789b171f064bd08c6f2aa66` (`origin/main`,
2026-09-06).

| Finding | Evidence | Consequence |
| --- | --- | --- |
| The authoritative journal and coordinator are bounded, validated primitives. | `sonder_runtime/application/memory/replication.py` defines `MemoryReplicationCoordinator`, `MemoryReplicationReceiver`, and `SQLiteMemoryReplicationSink`; `sonder_runtime/adapters/persistence/sqlite/memory_replication.py` persists journal pages. | These components can be reused, but only after a real source writer exists. |
| The outgoing peer client validates a bound receipt and refuses plain HTTP for remote origins. | `sonder_runtime/adapters/memory_replication/http_client.py` defines `HttpsMemoryReplicationSink`; `docs/runbooks/memory-replication.md` documents one bounded batch. | It is an adapter, not a configured runtime service. |
| The incoming route is disabled unless injected. | `sonder_runtime/interfaces/http/serve.py:146-161` defines `configure_memory_replication_receiver`; no normal composition root calls it. | A host cannot receive live replication batches today. |
| The canonical app constructs a memory facade but not a journal, coordinator, receiver, or projection. | `sonder_runtime/bootstrap/app.py:1183-1186` constructs `MemoryLearningFacade(UnitOfWorkAdapter, ...)`; a scoped source search found no normal-root instantiation of `SQLiteMemoryReplicationJournal`, `MemoryReplicationCoordinator`, or `MemoryReplicationReceiver`. | Normal application memory writes cannot produce a batch. |
| Legacy and canonical write paths are not one uniform mutation stream. | `sonder_runtime/adapters/memory_repository.py` delegates directly to `memory_store`; `sonder_runtime/adapters/memory_store.py` commits `log_interaction`, `add_fact`, `delete_fact`, preference writes, and outcome work through separate legacy routines. `server.py` also directly uses a unit of work for outcomes. | A post-hoc snapshot cannot safely infer ordering, versions, or deletions. |
| Journal records require source-owned ordering that the live store does not expose. | `MemoryMutation` requires source ID, epoch, sequence, entity version, operation, project, digest, and timestamp; `SQLiteMemoryReplicationJournal.append` accepts pre-assigned records. | A production writer needs a durable source identity, sequence allocator, version/tombstone rule, and atomic write boundary. |
| Applying a remote journal does not automatically update live memory. | `SQLiteMemoryReplicationProjection` is exercised in `tests/test_memory_replication_projection.py`, but no normal composition root creates it. | A valid receiver must only return a durable receipt after its configured target applies the record set according to an explicit projection contract. |
| Current configuration has no memory-replication section or secret. | `SonderConfig` contains `artifact_transfer`, `child_storage`, and `app_control`; `Secrets` contains no dedicated memory-replication credential. | There is no trusted-peer, scope, source identity, or credential admission boundary to compose. |
| Artifact transfer is a stronger comparison point but is not a memory transport. | `ArtifactTransferBinding` is built in `serve.configure_typed_config`; its dedicated key, fixed grant, private store, admission, and handler tests are in `tests/production/test_artifact_transfer_binding.py` and `tests/test_artifact_transfer_production_http.py`. | Its receiver is live and bounded, but no automatic memory/artifact migration may be inferred from that fact. |

The relevant existing test set was run from the isolated worktree at this
revision:

```text
83 passed in 22.16s
tests/test_memory_replication_coordinator.py
tests/test_memory_replication_http.py
tests/test_memory_replication_journal.py
tests/test_memory_replication_projection.py
tests/test_memory_learning_facade.py
tests/production/test_artifact_transfer_binding.py
tests/test_artifact_transfer_production_http.py
```

## Rejected configuration-only slice

Adding a `memory_replication` config section that merely constructs a
`MemoryReplicationReceiver` is intentionally rejected. It would make an HTTP
route reachable while all ordinary facts, interactions, outcomes, and
preferences continue to bypass the journal. The local journal would contain
no authoritative state, remote receipts would not update the normal memory
projection, and an operator could reasonably mistake the route for a working
replica. That is a capability claim this roadmap must not make.

Likewise, a periodic export of the legacy SQLite tables is rejected: the
tables do not expose a unified source epoch, sequence, version, or tombstone
stream. Reconstructing those values from snapshots would make concurrent
writes and deleted data ambiguous.

## Required contracts

The following four contracts are the minimum safe foundation before a runtime
composition patch is written.

### 1. Authoritative mutation contract

Define one supported write set and write it through a connection-bound journal
within the same SQLite transaction as its materialized state. The first
supported set must explicitly name every entity kind and deletion rule; the
existing projection recognizes `fact`, `interaction`, `outcome`, `preference`,
and `lesson_decision`.

For every supported mutation, persist:

```python
MemoryMutation(
    source_id=local_node_id,
    source_epoch=durably_persisted_epoch,
    sequence=durably_allocated_sequence,
    entity_kind=kind,
    entity_id=stable_id,
    version=monotonic_entity_version,
    operation="upsert" | "delete",
    project=exact_project_scope,
    payload=canonical_payload,
    recorded_at=utc_timestamp,
)
```

The writer must use the live `memory.db` connection or another explicitly
atomic durable-store boundary. It cannot append after a self-committing legacy
write and call the result atomic.

### 2. Receive-and-project receipt contract

The configured receiver must apply one validated batch to its local journal
and its live memory projection before it emits a durable `MemoryReplicaReceipt`.
If either durable action is unavailable or the projection rejects the batch,
the peer receives no success-shaped receipt. An idempotent retry may replay a
previous journal page; it must not bypass the projection cursor or entity
version checks.

The design must state whether the journal and projection share one database
transaction or use a recoverable two-stage protocol. If two-stage, the
incomplete state must be durable, retryable, and never counted as a receipt.

### 3. Fixed trusted-peer admission contract

Add a disabled-by-default typed section with a hard bound on peers. It needs,
at minimum:

- a bounded local node/source identity;
- an exact project scope, or an explicit declaration that the first release is
  global-only;
- an opt-in receiver flag and a bounded tuple of accepted remote source IDs;
- a dedicated replication credential, distinct from the general API and
  artifact-transfer credentials;
- an explicit outbound peer identity and origin for each permitted sink;
- bounded request, response, and batch limits; and
- validation that an enabled remote path satisfies the host's existing secure
  listener/proxy policy before a bearer is accepted or sent.

No membership discovery, peer enrollment, wildcard source IDs, caller-supplied
project selection, or ambient environment fallback belongs in this section.

### 4. Lifecycle and operator-evidence contract

The application composition root needs an owned replication service with
explicit start, bounded `replicate_once`, health/status, and close behavior.
It must expose only:

- local journal state and the last bounded attempt;
- validated durable receipt identities and cursors;
- pending or failed peer identities with stable reason codes; and
- an explicit `automatic_takeover_available: false` /
  `automatic_failback_available: false` posture.

It must not start a background retry loop, promote a node, change ownership,
infer quorum, or make a global scalability claim. A two-PC pair remains a
data-copy arrangement, never an independent-witness takeover system.

## Implementation sequence

1. **Complete locally for `fact` only.** The injected writer owns source
   identity, epoch, sequence, per-fact version, and tombstones. Direct calls
   own one SQLite transaction; injected unit-of-work calls use a source
   savepoint within an outer transaction opened lazily when the supported
   write is attempted. An untouched injected unit does not reserve SQLite's
   writer lock. In this fact-only path, a rollback after a supported write
   removes the fact, source state, journal record, and source cursor.
   Epoch advance is fail-closed unless the source cursor is bootstrap-safe:
   the journal has no records and `next_sequence == 1`. Pruning does not make
   an already allocated cursor eligible for rollover. Interactions, outcomes,
   preferences, and lessons remain outside this contract.
2. **Complete internally for `fact` only.** The explicitly constructed
   `SQLiteFactReplicationSink` rejects empty, out-of-bound, cross-scope, and
   non-fact pages before mutation. It uses one target `memory.db` connection
   for both remote journal evidence and `SQLiteMemoryReplicationProjection`,
   then creates a receipt only after that transaction commits. The test-only
   existing HTTP adapter proves a target normal fact row is visible before its
   matching receipt is framed, and a trigger-induced projection failure rolls
   back journal/projection state so the exact page can be retried. No
   configuration, listener, normal-root composition, background retry, or
   automatic mobility is implied.
3. Add the typed configuration and dedicated secret boundary. Prove disabled
   defaults, rejection of absent/empty or duplicate identities, cross-scope
   rejection, credential separation, bounded peers, and receiver unreachability
   without valid configuration.
4. Compose the owned service through every intended normal host root
   (`serve`, managed HTTP owner, standalone/Codex/Claude entrypoints). Prove a
   source write, a deliberate `replicate_once`, a peer read, and an honest
   pending state when the peer is stopped.
5. Add operator documentation and capability projection. Describe this as
   bounded authenticated replication and mobility evidence only; retain the
   existing no-takeover/no-failback/no-consensus/no-unbounded-scale statements.

## Artifact mobility boundary

The existing artifact receiver is genuinely composed behind a dedicated,
fixed-grant `ArtifactTransferBinding`, with byte digest verification and
bounded HTTP admission. It is still not an automatic artifact-mobility
service: there is no normal runtime component that selects an artifact,
creates a peer grant, sends it to a configured remote node, or treats a
receipt as failover evidence. Any next artifact slice should therefore use
one explicit, fixed peer grant and a caller-owned `replicate_once` command;
it must not be coupled to a memory receiver or presented as resource pooling.

## Current branch limits after Tasks 1–2

- The source contract supports only explicitly injected, project-scoped
  `fact` writes. Default and other legacy memory paths remain unjournaled.
- The receive contract exists only as an explicitly constructed internal
  fact-only sink. The existing HTTP adapter is exercised only by its test;
  no runtime listener, credential, trusted peer, lifecycle owner, or delivery
  loop composes it.
- A legacy self-committing operation used in the same injected unit of work
  can commit its outer transaction; this initial rollback guarantee is limited
  to the fact-only source path.
- No listener, credential, configuration, normal composition root, receiver,
  projection lifecycle, peer delivery, or retry behavior has changed.
- The branch does not claim that normal runtime memory is replicated, mobile,
  highly available, automatically recoverable, sharded, or infinitely
  scalable.
