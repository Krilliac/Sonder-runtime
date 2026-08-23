# Sonder schema migrations

One directory per SQLite store. Migration modules are named
`NNNN_description.py` and applied in lexical order by
`sonder_migrations.migrate_store()`. Each module defines:

```python
def preflight(conn) -> None: ...   # optional: refuse unsafe starting states
def apply(conn) -> None: ...       # required: runs inside one transaction
def verify(conn) -> None: ...      # optional: runs before the same commit
```

Applied migrations are recorded in the target database's
`schema_migrations` ledger with the SHA-256 of the migration source.
Never edit a migration after it has shipped — the checksum check will
refuse to proceed. Add a new migration instead.

The runner executes the exact byte string it checksummed and refuses a
source file that changes after discovery. Framework-owned migrations cannot
issue `BEGIN`, `COMMIT`, `ROLLBACK`, or `SAVEPOINT`; schema changes,
verification, and the ledger insert commit together. The ledger has database
triggers that reject updates and deletes. A small number of adoption
baselines declare `manages_own_transaction = True` because they call an older,
idempotent bootstrap through a second connection; after an interruption they
are safe to rerun until the append-only ledger insert succeeds.

Stores:

- `memory/`, `autopilot/`, `fleet/`, `operations/`, `queued_actions/`,
  `updates/`, and `jobs/` own their corresponding SQLite stores.
