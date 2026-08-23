# Backup and restore

Backups are consistent snapshots of every authoritative store
(memory.db, autopilot.db, fleet.db, operations.db, runtime_policy.json),
taken with the SQLite online-backup API, hash-verified, and published by
one atomic rename. A failed backup never prunes the last verified one.

## Create

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime backup create \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --json
```

The backup lands in `[backup].target` (default `/var/backups/sonder`)
as `<UTC timestamp>-<version>-<backup id>/` with `manifest.json` and
`checksums.sha256`. The run is recorded in operations.db.

## Verify

```bash
python -m sonder_runtime backup verify /var/backups/sonder/<backup-dir>
```

Verifies every manifest entry's presence, size, and SHA-256; rejects unlisted
state entries; parses the runtime policy; and opens each database read-only for
`PRAGMA quick_check`, foreign-key checks, and migration-ledger checksum/schema
comparison. Findings contain store names and error classes, never row content.

## List and prune

```bash
python -m sonder_runtime backup list --json
python -m sonder_runtime backup prune --keep 7
```

Prune never removes the newest verified backup, regardless of `--keep`.

## Monitoring backup health

```bash
python -m sonder_runtime doctor --json --skip-ollama
```

The `backup` check reads the most recent `backup_run` record in
operations.db (read-only; it never creates or migrates the database) and
reports `warn` when no backup has ever completed or the newest verified one
is older than 48 hours, and `fail` when the most recent run did not verify.
It reports `ok` without reading anything further when `[backup].enabled` is
`false`.

## Restore

Restoration targets an **empty** directory; it never overwrites a live
SONDER_HOME.

1. Stop the service: `sudo systemctl stop sonder`
2. Verify the chosen backup:
   `python -m sonder_runtime restore verify <backup-dir>`
3. Restore to a staging directory:
   ```bash
   sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime restore apply \
       <backup-dir> /var/lib/sonder-restore --confirm restore
   ```
   Restore copies into a sibling staging directory, re-hashes and fsyncs every
   file, fsyncs staging, and only then atomically publishes it. A copy or rename
   failure removes staging and leaves (or recreates) the original empty target.
4. Smoke-test the staged state:
   ```bash
   SONDER_HOME=/var/lib/sonder-restore \
   sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime smoke --skip-ollama
   ```
5. Swap directories with the service stopped:
   ```bash
   sudo mv /var/lib/sonder /var/lib/sonder-pre-restore
   sudo mv /var/lib/sonder-restore /var/lib/sonder
   ```
6. Start and verify: `sudo systemctl start sonder`, then
   `python -m sonder_runtime status --json`.

Keep `/var/lib/sonder-pre-restore` until the restored system has been
verified for as long as your incident policy requires.
