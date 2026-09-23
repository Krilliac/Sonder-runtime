# Authoritative fact migration

Live authoritative fact composition fails closed when a project contains a
fact without matching source state and journal evidence.  Existing facts are
adopted only by an operator running the bounded, explicit migration command.
The activation marker is not published when this gate fails, so a rejected
startup remains restartable and cannot advertise unjournaled rows as
authoritative.
The database must already have the current Sonder memory schema. The dry run
opens it read-only and refuses an older schema without initializing or
migrating it. Back up and upgrade an older schema through its separate
operator procedure before adopting facts.

First create a dry-run plan:

```text
python scripts/migrate_authoritative_facts.py --database ABSOLUTE_DB --source-id node-a --project repo-a
```

Review the returned count and digest.  Re-run with that exact digest and a new
backup path to apply the plan:

```text
python scripts/migrate_authoritative_facts.py --database ABSOLUTE_DB --source-id node-a --project repo-a --apply --digest DIGEST --backup ABSOLUTE_BACKUP_DB
```

The command adopts at most 1024 unjournaled facts and 32 MiB of stored fact
identity, project, text, and embedding bytes per plan. It rejects rows whose
stored types cannot be represented exactly in the journal and refuses a
changed plan, an existing backup, or a missing explicit backup.  The migration
requires an idle connection and treats the operator as responsible for
quiescing other writers. A backup is required for every apply. The snapshot is
written to a private temporary file in the requested directory, integrity
checked, and published with a no-clobber hard link; a filesystem without
same-directory hard-link support fails closed. Failed temporary backups are
removed. The plan digest binds the
source id, project scope, and exact row contents.  The command then acquires
`BEGIN IMMEDIATE` and re-reads the plan inside that write lock; a writer that
raced backup creation therefore causes a stale-plan refusal before any
mutation. Existing state rows, including tombstone-only rows, are rejected
before a migration plan or backup is approved when ownership differs or the
exact versioned journal upsert/delete evidence is missing. The canonical
journal digest and upsert text/embedding payload must also match the current
fact row; tombstones must have an empty payload. If the process is
interrupted after the write lock is acquired, the transaction is rolled back
and the connection is left idle so a fresh dry run can safely resume the
operation. Each adopted fact gets a version-one upsert record with empty
metadata; the fact row, source state, journal record, and derived indexes
commit together.  Any failure rolls all of those writes back.  A second plan
is empty after a successful migration, so restart and replay remain
deterministic.

## Older-schema database copies

An older memory database is adopted in this order, always on a copy with the
original left untouched:

1. Copy the database and keep a separate pre-upgrade backup of the copy.
2. Upgrade the copy's schema by opening it once with the normal Sonder memory
   store (this creates the authoritative state, activation, and journal tables
   and stamps the current schema version).
3. Starting the live composition root at this point is refused: the unit of
   work fails closed over the unjournaled facts and publishes no activation
   marker or journal row.
4. Run the dry run and explicit apply above with a second, new backup path.
5. Start the live composition root. Adopted facts are readable, new writes and
   deletes are journaled after them, and facts in other projects are left
   exactly as they were.

`tests/test_authoritative_live_fences.py` exercises this sequence through the
real `build_application` composition root and restart path, starting from the
original `facts` DDL with no authoritative tables.

## Alternate fact writers after activation

Once the live unit of work activates a project scope, the legacy
`memory_store.add_fact`/`delete_fact` helpers refuse that scope.  The
replication projection (used by the fact receiver sink and by projection
rebuild) also refuses to materialize, replace, or delete a fact in an
activated scope. The refusal happens inside the receiver's transaction, so the
receiver journal, projection log, cursor, fact rows, and derived indexes all
roll back and no receipt is issued.  Consequently a node cannot currently both
own a scope authoritatively and accept peer fact batches for the same scope;
multi-writer reconciliation for one scope is not supported.  Projection into
scopes that are not activated keeps its existing behavior.

Because a refused batch would otherwise be retried forever by its peer, the
receiver fails fast at startup instead. The live application graph always
activates the authoritative source for the configured replication scope and
memory database, so its replication service refuses to create a receiver when
`[memory_replication].receiver_enabled = true`, raising a `ConfigError` that
names the scope; HTTP startup then stops before the listener binds. A
standalone replication service also refuses a receiver database that already
carries an activation marker for the scope. Operators must disable
`receiver_enabled` on a node that owns the scope.

## Per-transaction activation cost

Every live unit of work re-enters activation under SQLite's writer lock.  The
full per-row journal authentication (canonical digest, payload, and embedding
comparison) runs only when a source first claims a scope, and during migration
planning and apply.  When the activation marker already names the same
source, each unit of work instead checks with indexed anti-joins that there are
no unowned or foreign rows, that every owned state row has its exact versioned
upsert or delete journal record, and that every live state row still has its
fact.  This check does not re-authenticate journal payload bytes on every
transaction; payload tampering by a raw SQL writer after activation is
detected only by a fresh claim or a migration plan.
