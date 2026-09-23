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
changed plan, an existing backup, or a missing explicit backup.  The backup is
created before the write transaction.  Each adopted fact gets a version-one
upsert record with empty metadata; the fact row, source state, journal record,
and derived indexes commit together.  Any failure rolls all of those writes
back.  A second plan is empty after a successful migration, so restart and
replay remain deterministic.
