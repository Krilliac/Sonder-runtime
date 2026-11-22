# Backups & Recovery

Consistent, verified, atomically-published backups of all authoritative
state (`sonder_runtime/adapters/backup.py`). A backup taken during live
activity is still consistent because SQLite databases are copied with the
online-backup API.

Commands, defaults, the exact list of stores captured, exit codes and the
restore procedure live in one place: the
[backup and restore runbook](../runbooks/backup-restore.md). This page keeps
the concepts. Last checked against the code: 2026-11-20.

## What a backup is

A directory `<target>/<UTC timestamp>-<version>-<backup_id>/` holding a
`manifest.json` (format version, ids, schema versions, per-file hashes), a
`state/` tree of copied stores and policy, and `checksums.sha256`. Which
stores are included is listed in the runbook, not here, so the list cannot
drift.

Backups are plaintext copies of private state: the checksums detect
corruption, not tampering, and there is no encryption or key-based
authentication (`SONDER_BACKUP_KEY_FILE` is refused at config load). Keep
the backup target private (the server installer creates `/var/backups/sonder`
with mode `0700`) or on an encrypted volume.

## How a backup is made

**Algorithm:** acquire the `backup` maintenance lock; reject concurrent
migration/restore/promotion/update; online-copy each live DB into a
staging dir; copy authoritative files; fsync; hash + manifest; verify
every staged file; run read-only SQLite quick/foreign-key checks and compare
the copied migration ledgers to the manifest; atomically rename staging →
final; record `verified`;
apply retention **only after** the new backup verifies. A failed backup
never prunes the last verified one.

**Tiered retention (GFS):** keeps the newest of each of the last N days,
weeks, and months (`[backup].retention_daily/weekly/monthly`), and always
the newest verified backup. A backup whose manifest has no parseable
`created_at_utc` (`created_at_valid: false` in `backup list --json`) is
listed last and belongs to no retention window.

## Restore

Restoration targets an **empty** directory and never clobbers a live
`SONDER_HOME`; swapping the restored directory into place is a
stopped-service operator step. Verification covers hashes, state inventory,
policy JSON, `PRAGMA quick_check`, foreign keys and migration-ledger health;
`restore smoke` additionally restores into a disposable directory and runs
each database's full `PRAGMA integrity_check` on the copy. The step-by-step
procedure, including the offline recovery rehearsal, is in the
[runbook](../runbooks/backup-restore.md#restore).

## Automation (server profile)

`packaging/systemd/` ships timers: `sonder-backup.timer` (daily create +
prune) and `sonder-restore-smoke.timer` (weekly restore-smoke on a
disposable directory) — so backups are proven restorable, not just taken.
The smoke targets the newest backup with a valid `created_at_utc`, selected
by `backup latest` (raw pre-epoch2 copies are skipped; the run fails when no
standard backup is dated); if that backup no longer verifies, the smoke fails
rather than silently picking an older one.

## Recovery scenarios

- **Corrupted store** — restore just that DB from the newest verified
  backup ([database-lock-or-corruption](../runbooks/database-lock-or-corruption.md)).
- **Disk exhaustion** — prune old releases/backups; the prune never
  removes the newest verified backup ([disk-exhaustion](../runbooks/disk-exhaustion.md)).
- **Failed upgrade** — the update engine takes a verified backup before
  installing, in `<state home>/backups`. Operator rollback switches code only
  and is refused when the previous release is missing; restoring state from
  that backup is a manual step
  ([upgrade-rollback](../runbooks/upgrade-rollback.md),
  [Update Manager](13-update-manager.md)).

Backup runs are recorded in `operations.db` (`BACKUP_COMPLETED` /
`BACKUP_FAILED`) with counts, hashes, and durations — never contents.
