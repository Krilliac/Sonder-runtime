# Backup and restore

Backups are consistent snapshots of every authoritative store
(memory.db, autopilot.db, fleet.db, operations.db, queued_actions.db,
updates.db, jobs.db, runtime_policy.json) plus, on an adopted home, the
epoch-2 domain databases `serve` requires (automation.db, selfmod.db,
training.db) and `epoch2_adoption_receipt.json`. Databases are copied with
the SQLite online-backup API, hash-verified, and published by one atomic
rename. A failed backup never prunes the last verified one.

## Working directory

Every `python -m sonder_runtime ...` command on this page must be run **from
`/opt/sonder/current`** on the packaged install (or from the checkout on a
source install). The release's virtual environment holds only the third-party
requirements and nothing installs `sonder_runtime` into it, so the module
resolves only when the working directory contains it; the systemd unit sets
`WorkingDirectory=/opt/sonder/current` for the same reason. Elsewhere the
command prints `No module named sonder_runtime` and exits 1. Commands below
that name only `python` stand for the release's
`/opt/sonder/current/venv/bin/python`.

**Identity and configuration.** `/var/backups/sonder` and `/var/lib/sonder`
are `sonder`-owned with mode `0700`, so run every command below as
`sudo -u sonder`. Every command that takes `--config` (`backup create`, `list`,
`latest`, `prune`, `doctor`, `status`) must be given
`--config /etc/sonder/sonder.toml`, plus `--secrets /etc/sonder/sonder.env`
where the secrets are needed. Without `--config`, `_configured_path` in
`sonder_runtime/__main__.py` uses the path in `SONDER_CONFIG` when that
variable is set and otherwise reads only `<default home>/sonder.toml` if it
exists, `[state].home` falls back to the invoking user's per-OS default home,
and `_backup_target` falls back to `<default home>/backups`. The result is a
false answer, not an error: `backup list` is empty, `backup latest` exits 1,
`doctor` warns that no backup exists, `status` reports an unrelated home and
`prune` acts on the wrong directory. `backup verify` and `restore` take no
`--config`; see their notes below.

## Create

```bash
cd /opt/sonder/current
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime backup create \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --json
```

The backup lands in the `--target` directory if given, else `[backup].target`,
as `<UTC timestamp>-<version>-<backup id>/` with `manifest.json` and
`checksums.sha256`. The run is recorded in operations.db. Expected output with
`--json`: `backup_id`, `path`, `files`, `total_bytes`.

Defaults (checked 2026-11-20, `sonder_runtime/platform/config.py` `BackupConfig`
and `sonder_runtime/__main__.py` `_backup_target`):

| Setting | Code default | Note |
|---|---|---|
| `[backup].enabled` | `true` | |
| `[backup].target` | `""` (empty) | empty means `<state home>/backups` |
| `retention_daily` / `_weekly` / `_monthly` | 7 / 4 / 6 | |

`/var/backups/sonder` is **not** a code default: it is the value in the
packaged `packaging/sonder.toml.example`, which the server installer creates
with mode `0700` and the systemd units may write to. A config that leaves
`target` empty backs up under `<state home>/backups`.

Exit codes: `0` success. `1` when `[backup].enabled = false` (the command
prints `backups are disabled in configuration ([backup].enabled = false)` and
does nothing; this applies to `create`, `list`, `latest` and `prune`, not to
`verify`), when the backup fails (`backup failed: ...`, including another
maintenance operation holding the lock), when `latest` finds no dated backup,
or when `verify` reports problems.

## Verify

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime \
    backup verify /var/backups/sonder/<backup-dir>
```

`backup verify` and every `restore` subcommand take no `--config` or
`--secrets`. `rehearse` is the odd one out only in how it is built: it creates
no default application (`__main__.py`), whereas the others build the default
application and resolve the state home from the environment
(`SONDER_STATE_HOME`/`SONDER_HOME`, else the per-OS default: on Linux
`$XDG_DATA_HOME/sonder`, else `$HOME/.local/share/sonder`). Run them as the
`sonder` user (the backup directory is `0700`) with `SONDER_HOME` set to the
service's `[state].home` if they must see the live home (see step 4 of Restore
for how to pass it through `sudo`). Expected output:
`backup verified`, exit 0; otherwise one `FAIL: ...` line per problem and
exit 1.

Verifies every manifest entry's presence, size, and SHA-256; rejects unlisted
state entries; parses the runtime policy; and opens each database read-only for
`PRAGMA quick_check`, foreign-key checks, and migration-ledger checksum/schema
comparison. Findings contain store names and error classes, never row content.

## List and prune

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime backup list \
    --config /etc/sonder/sonder.toml --json
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime backup prune \
    --config /etc/sonder/sonder.toml --keep 7
```

Prune never removes the newest verified backup, regardless of `--keep`.

`backup list` orders entries newest first by the parsed `created_at_utc`
instant. An entry whose manifest has no parseable timestamp reports
`"created_at_valid": false` and is listed after every dated backup,
pre-epoch2 copies included, so both prune modes treat it as the oldest: it
never takes a retention slot from a dated backup, and it is removed unless it
is the newest verified backup.

```bash
# bare path of the newest dated backup
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime backup latest \
    --config /etc/sonder/sonder.toml
# {"backup": {...list entry...}}
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime backup latest \
    --config /etc/sonder/sonder.toml --json
```

`backup latest` selects the first `created_at_valid` standard entry of that
listing (raw pre-epoch2 copies, described below, have no manifest to restore
and are skipped) and exits 1 when the target holds no dated standard backup.
The weekly `sonder-restore-smoke` unit passes its output to `restore smoke`.

The raw safety copies that `migrate --adopt-epoch2` writes to
`<state home>/backups/pre-epoch2-<UTC time>/` have no manifest, so they never
verify as restorable backups. They are listed with `"kind": "pre-epoch2"` and
take part in `--keep` and tiered retention, ranked by their timestamp among
all entries, with two guards. A pre-epoch2 copy is kept until a verified
standard backup newer than it exists, because until then it is the only copy
of the pre-adoption state. And a pre-epoch2 copy never takes a `--keep` slot
or a daily/weekly/monthly bucket away from a standard backup: standard
backups are retained exactly as if no pre-epoch2 copies existed. Re-running `migrate --adopt-epoch2` on a home that is already adopted
and verified is a no-op (`"already_adopted": true`) and takes no new copy.

## Monitoring backup health

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime doctor \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env \
    --json --skip-ollama
```

The `backup` check reads the most recently started `backup_run` record in
operations.db (`ORDER BY started_at_utc DESC LIMIT 1`; read-only, it never
creates or migrates the database) and reports `warn` when no backup has ever
been started, when that latest run is still `running`, or when it is
`verified` and older than 48 hours, and `fail` when it finished with any
status other than `verified`.
It reports `ok` without reading anything further when `[backup].enabled` is
`false`.

## Restore

Restoration targets an **empty** directory; it never overwrites a live
SONDER_HOME.

1. Stop the service: `sudo systemctl stop sonder`
2. Verify the chosen backup, then smoke-restore it into a disposable
   directory:
   ```bash
   sudo -u sonder env SONDER_HOME=/var/lib/sonder \
       /opt/sonder/current/venv/bin/python -m sonder_runtime restore verify <backup-dir>
   sudo -u sonder env SONDER_HOME=/var/lib/sonder \
       /opt/sonder/current/venv/bin/python -m sonder_runtime restore smoke <backup-dir>
   ```
   They print `backup verified` and `restore smoke passed`. Use the `sonder`
   identity because the backup directory is `0700`, and `env SONDER_HOME=...`
   after `sudo` because `restore` takes no `--config`
   (`/var/lib/sonder` is the packaged `[state].home`; use yours if it differs).
   `restore smoke` fails when the restored home would be refused by `serve`
   (an adopted memory.db without every epoch-2 domain database, as in backups
   taken before those databases were captured). A wholly pre-epoch2 backup
   passes the smoke but needs `migrate --adopt-epoch2` against the restored
   home before step 6.
3. Restore to a staging directory. The destination's **parent** must be
   writable by the user running the command: `restore_to_empty` creates its
   staging directory beside the destination with `tempfile.mkdtemp(dir=<parent>)`
   (`sonder_runtime/adapters/backup.py`). `/var/lib` is root-owned `0755`
   and the installer creates only `/var/lib/sonder` for `sonder`
   (`packaging/install_sonder.sh`), so a destination directly under `/var/lib`
   fails with a permission error when run as `sonder` (read from the code, not
   exercised). Create a `sonder`-owned parent on the same filesystem as
   `/var/lib/sonder` so the step-5 `mv` is a rename:
   ```bash
   sudo install -d -o sonder -g sonder -m 0700 /var/lib/sonder-restore
   sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime restore apply \
       <backup-dir> /var/lib/sonder-restore/home --confirm restore
   ```
   Restore copies into a sibling staging directory, re-hashes and fsyncs every
   file, fsyncs staging, and only then atomically publishes it. A copy or rename
   failure removes staging and leaves (or recreates) the original empty target.
4. Smoke-test the staged state:
   ```bash
   cd /opt/sonder/current
   sudo -u sonder env SONDER_HOME=/var/lib/sonder-restore/home \
       /opt/sonder/current/venv/bin/python -m sonder_runtime smoke --skip-ollama
   ```
   Expected: `smoke passed`, exit 0.

   - Put the variable **after** `sudo`. `sudo` normally resets the environment
     (`env_reset`), so `SONDER_HOME=... sudo -u sonder ...` is dropped.
   - **A dropped variable gives a false pass, not an error.** `smoke` given no
     `--config` uses `SONDER_CONFIG` when that variable is set and otherwise
     loads `<default home>/sonder.toml` only if that file exists
     (`_configured_path` in `sonder_runtime/__main__.py`); unless
     `SONDER_CONFIG` points at it, it does not read
     `/etc/sonder/sonder.toml`, so it never learns `[state].home`. The home
     then falls back to the per-OS default (`$XDG_DATA_HOME/sonder`, else
     `$HOME/.local/share/sonder`; `default_home` in
     `sonder_runtime/platform/paths.py`), which is a home unrelated to both
     the live state and the restored directory (what `$HOME` is after `sudo`
     depends on your sudoers settings). `smoke` migrates and writes a new
     operations store there (`cmd_smoke`) and can print `smoke passed`
     without ever touching the restored state.
   - `smoke` is **not read-only**: it migrates the operations store and writes
     a `SMOKE_EVENT` into the home it resolves. Pointed at the restored
     directory that is harmless; pointed at the live home it writes to live
     state.
   - The process environment beats the config file: `load_config` applies
     the environment after the config and secrets files (only explicit `--set`
     overrides come later) and `_apply_environment` replaces `[state].home`
     with `SONDER_STATE_HOME`, else `SONDER_HOME`
     (`sonder_runtime/platform/config.py`, `sonder_runtime/platform/paths.py`),
     so `SONDER_HOME` wins even if `SONDER_CONFIG` points at
     `/etc/sonder/sonder.toml`. The config's `[state].home` applies only when
     `SONDER_HOME` is absent, for example when `sudo` dropped it, which is
     how the smoke ends up on the live home. Check the variable is present
     after `sudo`.
   - The same home resolution applies to `update` commands; see
     [upgrade-rollback](upgrade-rollback.md#operator-conventions-read-first) (Operator
     conventions section there).
5. Swap directories with the service stopped:
   ```bash
   sudo mv /var/lib/sonder /var/lib/sonder-pre-restore
   sudo mv /var/lib/sonder-restore/home /var/lib/sonder
   sudo rmdir /var/lib/sonder-restore
   ```
6. Start and verify: `sudo systemctl start sonder`, then
   ```bash
   sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime status \
       --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --json
   ```

Keep `/var/lib/sonder-pre-restore` until the restored system has been
verified for as long as your incident policy requires.

## Offline recovery rehearsal

Run the disposable rehearsal against a verified backup before an upgrade or
recovery exercise.  This is a local/test-only contract: it does not stop the
service, switch `current`, contact a provider, or provide live failover.

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime restore rehearse \
    /var/backups/sonder/<verified-backup> \
    --workspace /var/tmp/sonder-recovery-rehearsal \
    --source-revision "<release revision recorded before the upgrade>" --json
```

`--workspace` must be an existing regular directory (a symlink is refused);
without it the command uses a fresh temporary directory and removes it when
empty.  `--source-revision` makes the drill refuse a backup that records a
different revision; without it the backup's own recorded revision is used.
The candidate upgrade is labelled `<source>+rehearsal` unless
`--target-revision` names one.  The command exits 0 with the report below,
1 when the rehearsal refuses the backup (the JSON names the error and the
steps completed before it), and 2 for an unusable workspace or request.

The same drill from Python, for a caller composing its own port:

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
