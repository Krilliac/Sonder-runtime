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
mutation.  Each adopted fact gets a version-one upsert record with empty
metadata; the fact row, source state, journal record, and derived indexes
commit together.  Any failure rolls all of those writes back.  A second plan
is empty after a successful migration, so restart and replay remain
deterministic.
