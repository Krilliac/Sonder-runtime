# Authoritative fact migration

Live authoritative fact composition fails closed when a project contains a
fact without matching source state and journal evidence.  Existing facts are
adopted only by an operator running the bounded, explicit migration command.

First create a dry-run plan:

```text
python scripts/migrate_authoritative_facts.py --database ABSOLUTE_DB --source-id node-a --project repo-a
```

Review the returned count and digest.  Re-run with that exact digest and a new
backup path to apply the plan:

```text
python scripts/migrate_authoritative_facts.py --database ABSOLUTE_DB --source-id node-a --project repo-a --apply --digest DIGEST --backup ABSOLUTE_BACKUP_DB
```

The command adopts at most 1024 unjournaled facts per plan.  It refuses a
changed plan, an existing backup, or a missing explicit backup.  The migration
requires an idle connection and treats the operator as responsible for
quiescing other writers.  The backup path is created exclusively, integrity
checked, and removed if backup creation fails.  The plan digest binds the
source id, project scope, and exact row contents.  The command then acquires
`BEGIN IMMEDIATE` and re-reads the plan inside that write lock; a writer that
raced backup creation therefore causes a stale-plan refusal before any
mutation.  Each adopted fact gets a version-one upsert record with empty
metadata; the fact row, source state, journal record, and derived indexes
commit together.  Any failure rolls all of those writes back.  A second plan
is empty after a successful migration, so restart and replay remain
deterministic.
