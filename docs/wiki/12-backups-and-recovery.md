# Backups & Recovery

Consistent, verified, atomically-published backups of all authoritative
state (`sonder_backup.py`). A backup taken during live activity is still
consistent because SQLite databases are copied with the online-backup API.

## What a backup contains

```
<target>/<UTC timestamp>-<version>-<backup_id>/
  manifest.json          # format version, ids, schema versions, per-file hashes
  state/
    memory.db  autopilot.db  fleet.db  operations.db  updates.db
    runtime_policy.json
  checksums.sha256
```

Backups are plaintext copies of private state: the checksums detect
corruption, not tampering, and there is no encryption or key-based
authentication (`SONDER_BACKUP_KEY_FILE` is refused at config load). Keep
the backup target private (the server installer creates `/var/backups/sonder`
with mode `0700`) or on an encrypted volume.

## Create / verify / list / prune

```bash
python -m sonder_runtime backup create --json     # -> backup_id, path, files, bytes
python -m sonder_runtime backup verify <dir>      # hashes + SQLite/schema/policy integrity
python -m sonder_runtime backup list --json
python -m sonder_runtime backup latest            # path of the newest dated backup
python -m sonder_runtime backup prune             # tiered retention (default)
python -m sonder_runtime backup prune --keep 7    # simple keep-N
```

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

Restoration targets an **empty** directory — it never clobbers a live
`SONDER_HOME`.

```bash
# 1. Verify, then restore to staging:
python -m sonder_runtime restore verify <backup-dir>
python -m sonder_runtime restore apply  <backup-dir> /var/lib/sonder-restore --confirm restore
# 2. Prove the restored state is usable BEFORE switching:
python -m sonder_runtime restore smoke  <backup-dir>
```

Normal verification already checks hashes, state inventory, policy JSON,
`PRAGMA quick_check`, foreign keys, and migration-ledger health. `restore
smoke` additionally restores into a disposable directory and runs each DB's
full `PRAGMA integrity_check` against the copied result.
Swapping the restored directory into place is a stopped-service operator
step ([backup-restore](../runbooks/backup-restore.md)).

`python -m sonder_runtime restore rehearse <backup-dir>` runs the offline
recovery rehearsal against a verified backup in a disposable workspace. It
never opens the live state home, stops the service or switches `current`;
flags, exit codes and output are in the runbook's
[offline recovery rehearsal](../runbooks/backup-restore.md#offline-recovery-rehearsal)
section.

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
  installing; rollback restores from it when a code-only rollback is
  insufficient ([Update Manager](13-update-manager.md)).

Backup runs are recorded in `operations.db` (`BACKUP_COMPLETED` /
`BACKUP_FAILED`) with counts, hashes, and durations — never contents.
