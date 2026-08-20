# DATA-001 — Per-domain persistence ownership

This slice closes the previously identified DATA-001 contract gap by adding
`sonder_runtime/application/persistence/domain_ownership.py`.  It inventories
the existing SQLite adapters without importing SQLite or creating connections
from the application layer, and validates the concrete filesystem store
identity through `DomainStoreRegistry`.

Each persistent source-of-truth domain now has explicit metadata for:

- exactly one database and repository owner;
- its migration-store name and `schema_migrations` ledger;
- the local transaction boundary, including the domain mutation and outbox
  append;
- transactional-outbox integration and at-least-once projection semantics;
- legacy database names where epoch-2 adoption consolidates ownership.

`DomainStoreRegistry` is the concrete path-ownership gate.  It canonicalizes
relative paths, parent-directory aliases, existing symlink aliases, and
Windows case aliases without opening or creating a database.  It rejects
in-memory/URI targets, non-SQLite suffixes, duplicate domains, and any two
domains resolving to the same SQLite path.  Its read-only `domain_to_path` and
`path_to_domain` mappings provide both directions of the owner proof, while
`validate_ownership` checks that the path registry agrees with the repository
and logical filename declarations.

The contract rejects cross-database transaction claims.  Cross-domain work is
represented as an application workflow or durable event, and `operations.db`
is documented as a projection/event-import owner rather than a transaction
participant for another source-of-truth domain.  The module is metadata-only;
the concrete adapters in `sonder_runtime/adapters/persistence/sqlite/` retain
all SQL, connection, migration, and commit authority.

Focused coverage is in `tests/test_remaining_domain_ownership.py`, including
one-to-one owner mapping, alias collision rejection, ambiguous-target
rejection, and declaration-drift rejection.  The tests use temporary paths
only; no user database is opened or modified.  Formal
specification checkboxes are intentionally unchanged; this evidence closes
the contract portion of DATA-001 but does not claim DATA-002 or DATA-003
completion by itself.
