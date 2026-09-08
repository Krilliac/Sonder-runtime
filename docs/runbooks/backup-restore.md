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

## Offline recovery rehearsal

Run the disposable rehearsal against a verified backup before an upgrade or
recovery exercise.  This is a local/test-only contract: it does not stop the
service, switch `current`, contact a provider, or provide live failover.

```python
from pathlib import Path

from sonder_runtime.adapters.updates.offline_rehearsal import (
    FilesystemOfflineRecoveryPort,
)
from sonder_runtime.application.updates.recovery_rehearsal import (
    OfflineRecoveryRehearsal, OfflineRehearsalRequest,
)

backup = "/var/backups/sonder/<verified-backup>"
workspace = Path("/var/tmp/sonder-recovery-rehearsal")
workspace.mkdir(parents=True, exist_ok=True)
port = FilesystemOfflineRecoveryPort(workspace)
manifest = port.inspect_backup(backup)
known_source_revision = "<release revision recorded before the upgrade>"
if manifest.source_revision != known_source_revision:
    raise RuntimeError("selected backup is not the expected source revision")
report = OfflineRecoveryRehearsal(port).run(OfflineRehearsalRequest(
    backup_ref=backup,
    destination_ref=str(workspace / "run-1"),
    source_revision=known_source_revision,
    target_revision="candidate-release-revision",
))
print(report.as_dict())
```

The default local adapter deliberately reports a failed candidate upgrade so
the rollback and state-restore path is exercised.  The report must show the
manifest/checksum digests, `rollback_verified=true`, the ordered steps, and a
complete bounded cleanup receipt.  A mismatched source revision or corrupt
manifest member is refused before staging.  The contract is evidence for a
disposable drill; it is not evidence that a second host can take over.
