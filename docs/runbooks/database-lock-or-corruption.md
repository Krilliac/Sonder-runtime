# Database lock or corruption

## Lock storms (SQLITE_BUSY)

Symptoms: slow requests, `sonder_sqlite_lock_wait_seconds` climbing,
"database is locked" in logs.

1. Identify the store from log context (memory/autopilot/fleet/operations).
2. Look for a stuck process holding a long transaction:
   `fuser /var/lib/sonder/<store>.db` (or lsof).
3. A drain + restart clears in-process contention:
   `systemctl restart sonder` (this drains first).
4. Persistent contention is a bug — capture `/health`, the journal, and
   file an issue; do not raise busy_timeout past 30s as a fix.

## Corruption

Symptoms: "database disk image is malformed", integrity errors in
diagnostics, restore-smoke failures.

1. Stop the service: `systemctl stop sonder`
2. Confirm:
   ```bash
   sqlite3 /var/lib/sonder/<store>.db "PRAGMA integrity_check;"
   ```
3. Do **not** attempt in-place repair on the live file. Copy it aside
   first: `cp <store>.db <store>.db.corrupt-$(date -u +%Y%m%dT%H%M%SZ)`
4. Restore from the newest verified backup (backup-restore.md). A single
   corrupted store can be restored alone: restore to staging, copy only
   that store's file in, keeping the rest of the live state.
5. Run `python -m sonder_runtime restore smoke` against the staged state
   before switching, then start and verify `/ready`.
6. Record the incident and check the disk (smartctl, dmesg) — SQLite
   corruption on a healthy host is rare; suspect hardware or power loss.
