# DATA-001 — Per-domain persistence ownership

This slice closes the previously identified DATA-001 contract gap by adding
`sonder_runtime/application/persistence/domain_ownership.py`.  It inventories
the existing SQLite adapters without importing SQLite or creating connections
from the application layer.

Each persistent source-of-truth domain now has explicit metadata for:

- exactly one database and repository owner;
- its migration-store name and `schema_migrations` ledger;
- the local transaction boundary, including the domain mutation and outbox
  append;
- transactional-outbox integration and at-least-once projection semantics;
- legacy database names where epoch-2 adoption consolidates ownership.

The contract rejects cross-database transaction claims.  Cross-domain work is
represented as an application workflow or durable event, and `operations.db`
is documented as a projection/event-import owner rather than a transaction
participant for another source-of-truth domain.  The module is metadata-only;
the concrete adapters in `sonder_runtime/adapters/persistence/sqlite/` retain
all SQL, connection, migration, and commit authority.

Focused coverage is in `tests/test_remaining_domain_ownership.py`.  Formal
specification checkboxes are intentionally unchanged; this evidence closes
the contract portion of DATA-001 but does not claim DATA-002 or DATA-003
completion by itself.
