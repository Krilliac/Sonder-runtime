# Sonder schema migrations

One directory per SQLite store. Migration modules are named
`NNNN_description.py` and applied in lexical order by
`sonder_migrations.migrate_store()` (see
`sonder_runtime/adapters/persistence/migrations.py`). Each module defines:

```python
def preflight(conn) -> None: ...   # optional: refuse unsafe starting states
def apply(conn) -> None: ...       # required: runs inside one transaction
def verify(conn) -> None: ...      # optional: runs before the same commit
```

A module may instead set `manages_own_transaction = True` at module scope
when it delegates to an idempotent legacy bootstrap (e.g. a store module's
own `init_db`/`_migrate`) that owns its own transaction — used by each
store's `0001_baseline` adoption migration and by later migrations that
still route schema changes through that store's existing bootstrap
(e.g. `memory/0002_outcomes_source.py`).

Applied migrations are recorded in the target database's
`schema_migrations` ledger with the SHA-256 of the migration source.
Never edit a migration after it has shipped — the checksum check will
refuse to proceed (`MigrationError`). Add a new migration instead.

There is no down/rollback migration mechanism: a migration either commits
in full (schema change + ledger row, in one transaction) or is rolled back
entirely on error, leaving the database exactly as it was. To undo an
applied migration, ship a new forward migration that reverses the change.
A database whose ledger names a migration this build does not define (a
downgrade, or a build that shipped without its `migrations/` directory) is
refused with `FutureSchemaError` rather than silently accepted.

Every store below has a `0001_baseline` migration and reports a full
ledger via `sonder_migrations.status_all()` / `status_all_read_only()`;
none of them run on legacy ad-hoc bootstraps any more. `sonder migrate`
applies pending migrations for one `--store` or all of them; `sonder
doctor`'s schema check reports pending/modified/future-schema counts
without mutating anything.

The runner executes the exact byte string it checksummed and refuses a
source file that changes after discovery. Framework-owned migrations cannot
issue `BEGIN`, `COMMIT`, `ROLLBACK`, or `SAVEPOINT`; schema changes,
verification, and the ledger insert commit together. The ledger has database
triggers that reject updates and deletes. A small number of adoption
baselines declare `manages_own_transaction = True` because they call an older,
idempotent bootstrap through a second connection; after an interruption they
are safe to rerun until the append-only ledger insert succeeds.

Stores:

- `memory/` — memory.db (also carries the separately invoked SPEC-5 bridge's
  `schema_epoch` marker)
- `autopilot/` — autopilot.db
- `fleet/` — fleet.db
- `operations/` — operations.db
- `queued_actions/` — queued_actions.db
- `updates/` — updates.db
- `jobs/` — jobs.db
