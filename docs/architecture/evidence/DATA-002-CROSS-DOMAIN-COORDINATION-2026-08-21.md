# DATA-002 cross-domain coordination evidence — 2026-08-21

## Scope

This slice connects the existing transaction-neutral `TransactionNeutralRecord`,
immutable `OutboxEvent`, and `OutboxCASRepository` shapes to a typed
`SQLiteCrossDomainCoordinator`. It is limited to data/outbox application and
SQLite adapter code plus focused tests.

## Semantics

- **Atomicity:** participants must share the coordinator's SQLite database and
  caller-owned `BEGIN IMMEDIATE` transaction. Every record and matching outbox
  row, plus the operation receipt, commits together; any revision conflict or
  SQLite error rolls back all domains.
- **Idempotency:** an operation ID stores a canonical write-set fingerprint.
  Repeating the exact operation returns a replay result without writing again;
  reusing the ID for a different write set raises a typed conflict.
- **Failure:** stale expected revisions fail closed with no partial domain
  tables or operation receipt. Separate databases are intentionally not
  presented as atomically coordinated by this adapter.
- **Ownership:** each domain supplies its record/event values; the coordinator
  owns only the cross-domain transaction and receipt. It does not interpret
  domain payloads or dispatch events.

## Evidence

`tests/test_data002_cross_domain_coordination.py` covers multi-domain commit,
exact replay, fingerprint conflict, and rollback after a later participant's
revision failure. Focused result on 2026-08-21: 3 passed.
