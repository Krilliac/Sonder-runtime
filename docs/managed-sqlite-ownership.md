# Managed SQLite ownership dependency

The explicit `owned_sqlite.connect` factory delegates unchanged to native SQLite unless private disposable-process composition installs an owner. Managed mode retains each exact connection and constructing Thread object until successful close. It bounds connection admission, refuses new opens after stop, rejects out-of-namespace paths, and preserves unresolved transaction/handle cleanup. It does not monkeypatch sqlite3 or discover unknown native handles.

Use `owned_sqlite.transaction` for operation-scoped connections: it retains native commit/rollback semantics and closes in finally. A bare SQLite connection context manager does not close the connection. CAS, loop state/retry, cross-domain and extension stores use the scoped form, including initialization and failure/replay paths.

Durable spill output, persistent terminal journals and workflow checkpoints also scope their connection helpers. Agent-lane reads and projection updates use an explicit commit/rollback/close scope; its public raw `connect()` API remains caller-owned and closes failed initialization before returning. Early workflow CAS refusal ends its transaction and closes the connection. Spill commits remain one-shot; a repeated commit rejects without changing the persisted artifact.

CheckpointStore scopes every operation under its existing lock. File-backed state persists in the same database. In-memory mode retains canonical checkpoint rows between temporary connections; it does not retain a worker-owned handle. The in-memory path copies the current rows per operation and is intended for transient small checkpoint sets; file-backed mode avoids that copying. A failed operation preserves the previous in-memory rows. `close()` retains its existing behavior: clear in-memory state, while file-backed data remains available to a later operation or store.

## Activation boundary still required

This dependency alone does not enable a managed runtime or certify complete Application closure. Thread-local caches (including embedding cache, composition store and sqlite_factory cached connections) require an exact current-thread cleanup finalizer at every managed HTTP/probe/worker exit. If a worker terminates without that finalizer, its strongly retained connection remains occupied and cleanup cannot be certified from another thread. The owner must refuse clean state/activation; Thread death, process containment, path absence or a generic provider close is not a substitute.

The managed foreground owner must also prove HTTP/worker/provider admissions stopped, active operations complete, every non-SQLite resource closed, exact process/Job cleanup, current config policy and child-storage selection. It must not infer those proofs from this registry. Unmanaged runtimes are not adopted or inspected by it.

## Evidence

Actual isolated subprocess tests execute repeated extension, CAS, loop/retry and cross-domain operations under a four-handle capacity, including replay and conflict rollback; every operation leaves zero handles. File and in-memory checkpoint tests first use the store on a worker, then read/close it on the main thread. Separate tests verify failed checkpoint write rollback, stopped-constructor races, foreign-thread refusal, uncommitted transaction classification and missing native-handle receipt retention. Static construction coverage supports those runtime tests; it is not an unknown-handle census.

The same low-capacity matrix covers twelve cycles each of spill writes/reopen/read/commit refusal, terminal append/duplicate creation/reopen, workflow CAS/reopen/early return, and lane reads/projection. Each helper also rolls back an actual inserted row after an injected exception and releases its handle. Lane initialization refusal uses a real SQLite authorizer rejecting PRAGMA and must leave no tracked connection.
