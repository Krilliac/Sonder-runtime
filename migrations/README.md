# Sonder schema migrations

One directory per SQLite store. Migration modules are named
`NNNN_description.py` and applied in lexical order by
`sonder_migrations.migrate_store()`. Each module defines:

```python
def preflight(conn) -> None: ...   # optional: refuse unsafe starting states
def apply(conn) -> None: ...       # required: runs inside one transaction
def verify(conn) -> None: ...      # optional: post-commit assertion
```

Applied migrations are recorded in the target database's
`schema_migrations` ledger with the SHA-256 of the migration source.
Never edit a migration after it has shipped — the checksum check will
refuse to proceed. Add a new migration instead.

Stores:

- `operations/` — operations.db (SPEC-2 WP1 baseline exists)
- `memory/`, `autopilot/`, `fleet/` — baselines arrive with SPEC-2 WP5;
  until then those stores keep their legacy ad-hoc bootstrap and the
  framework reports them as having no ledger.
