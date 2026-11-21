# Upgrade and rollback

Updates install from signed bundles into versioned release directories
and activate by an atomic pointer switch. The active release is never
modified in place, and the previous release is retained.

## Build a bundle (publisher side)

Run this from the checkout's own virtual environment (the interpreter that
has the runtime's dependencies), **with the checkout as the working
directory**. The repository has no `pyproject.toml` or `setup.py` and the
virtual environment only holds the third-party requirements, so
`-m sonder_runtime` resolves only from the directory that contains the
`sonder_runtime` package. From anywhere else it fails with
`No module named sonder_runtime` and exit 1.

```bash
cd /path/to/checkout
/path/to/checkout/venv/bin/python -m sonder_runtime update build \
    /path/to/checkout /path/to/out --bundle-version 1.4.0 --channel stable
```

Produces `sonder-engine-<version>.tar.gz` and `manifest.json` (lengths,
SHA-256 per file, platform/arch, schema targets, health checks). For a
production channel the bundle directory must also carry TUF `metadata/`
signed through the threshold ceremony — an unsigned bundle is refused
unless explicitly allowed for development.

## Operator conventions (read first)

`update` is a different kind of command from `serve` and `backup`. It takes
**no** `--config`, `--secrets` or `--set`. It finds its state only from the
environment: the state home from `SONDER_STATE_HOME`, then `SONDER_HOME`, then
the per-OS default (`update` loads no configuration file; `serve` resolves
`SONDER_STATE_HOME` and `SONDER_HOME` in the same order; see [wiki 03](../wiki/03-configuration.md#state-home)); the release layout from `SONDER_MACHINE_HOME` (default
`/opt/sonder` on Linux and macOS, `%PROGRAMDATA%\Sonder` on Windows),
`SONDER_RELEASES_DIR` (default `<machine home>/releases`) and
`SONDER_CURRENT_LINK` (default `<machine home>/current`). Code:
`sonder_runtime/__main__.py` `cmd_update`, `sonder_runtime/platform/paths.py`
`default_home` and `default_machine_home`,
`sonder_runtime/adapters/updates/engine.py` `default_releases_dir` and
`default_current_link` (checked 2026-11-20).

The service's state home comes from `[state].home` in `sonder.toml`, which
`update` does not read. With no override `update` resolves the per-OS default
(on Linux `$XDG_DATA_HOME/sonder`, else `$HOME/.local/share/sonder`), which is
a home other than `[state].home` and has its own, empty `updates.db`. So set
`SONDER_HOME` to the same value as `[state].home` on every `update` command.
`sudo` normally drops variables set before it (`env_reset`), so pass the
variable through `env` after `sudo`, as below. What `$HOME` is after `sudo`
depends on your sudoers `env_reset` / `env_keep` settings, so do not rely on
it. `/var/lib/sonder` below is the packaged default of `[state].home`
(`packaging/sonder.toml.example`); change it if your config differs.

**Run `update` as root, not as `sonder`.** The installer creates
`/opt/sonder` and `/opt/sonder/releases` as root with mode 0755
(`packaging/install_sonder.sh`), and `/opt/sonder/current` is a root-owned
symlink; nothing in `packaging/` changes that
([install-server-private](install-server-private.md) lists the same
owners). `install` first takes `releases/.activation.lock`, then extracts into
`releases/.staging-<id>` and renames it there, `rollback` takes the same lock,
and both switch the
`current` pointer by creating a temporary link next to it in `/opt/sonder`
(`sonder_runtime/adapters/updates/engine.py`, `service.py`
`switch_active_pointer`; read from the code 2026-11-20, not exercised). The
`sonder` user can write none of those, so `install` and `rollback` run as
`sonder` stop with a permission error naming a path under `/opt/sonder`.
Root is the one identity whose permissions fit the packaged layout. See the
ownership open item below for the side effect on the state home.

Pin the interpreter **before** you start, and work from the release
directory. The install switches `/opt/sonder/current`, so a path through
`current` names a different release afterwards (see the open items below).
`-m sonder_runtime` resolves only when the working directory contains the
`sonder_runtime` package (the release's venv holds only third-party
requirements, and the systemd unit sets `WorkingDirectory=/opt/sonder/current`
for the same reason). From any other directory it prints
`No module named sonder_runtime` and exits 1:

```bash
OLD_RELEASE="$(readlink -f /opt/sonder/current)"
PY="$OLD_RELEASE/venv/bin/python"
UPD="sudo env SONDER_HOME=/var/lib/sonder $PY -m sonder_runtime update"
cd "$OLD_RELEASE"
test -x "$PY" && echo "interpreter ok: $PY"
```

Expected: `interpreter ok: <path>`. If nothing prints, the path is wrong; fix it
before continuing. Keep this shell and its working directory for every command
below. The commands use `$UPD <subcommand>`; the variable holds the whole
prefix, so keep it unquoted.

## Upgrade (operator side)

```bash
# 1. Import and verify (trust, hashes, compatibility):
$UPD import /media/sonder-offline-update --channel stable
# -> reports update_id, status=available, confirm_nonce (exit 0);
#    any other status exits 1 and installs nothing.

# 2. Review what will happen (this subcommand always prints JSON):
$UPD status

# 3. Stop the service, then install with explicit confirmation:
sudo systemctl stop sonder
$UPD install <update-id> --confirm <nonce>
# -> on success plan status "committed" and exit 0; a refusal exits 1.
#    A permission error naming a path under /opt/sonder means this was
#    not run as root (see Operator conventions); nothing was installed.

# 4. Before starting, check the new release can run (see the open item):
test -x /opt/sonder/current/venv/bin/python && echo "venv present" || echo "venv MISSING"

# 5. Start on the new release and verify:
sudo systemctl start sonder
curl -s http://127.0.0.1:11435/version
$UPD status
```

(`11435` is the default `[server].port`; use yours if you changed it.)

The install runs, in order: trust revalidation, staged extraction with
manifest verification, compatibility preflight, **verified backup**,
drain step, atomic staging publish, `venv` carry-forward from the active
release (`_carry_venv_forward`), migrations executed by the *target*
release, manifest health checks, atomic `current` switch, commit. Every
step is journaled in updates.db, except that a successful `venv`
carry-forward records nothing (a `venv-link` row is written only on
failure); a migration or health failure leaves
the previous release active and the failed release retained as evidence.

**The drain step is a no-op for the CLI.** The command-line install has no
running service attached, so the journal records the drain as `skipped` with
reason `no running service attached`
(`sonder_runtime/adapters/updates/engine.py`, the `drain` step of `install`).
Nothing drains the service for you: stop it first, as step 3 does (or run
`python -m sonder_runtime drain` and wait for the server to exit, see
[start-stop-drain](start-stop-drain.md)). If the service keeps running while
the release changes, in-flight work is not protected.

**The `venv` is carried forward, not rebuilt.** The systemd unit starts
`/opt/sonder/current/venv/bin/python`. `update build` excludes `venv` from the
bundle (`DEFAULT_BUNDLE_EXCLUDES` in
`sonder_runtime/adapters/updates/service.py`), so after the atomic publish and
before the migrations the engine symlinks `<new release>/venv` to the active
release's resolved `venv` (`_carry_venv_forward` in
`sonder_runtime/adapters/updates/engine.py`) and rolls the activation back with
`VENV_LINK_FAILED` if the link cannot be made. The engine creates no virtual
environment and installs no packages, so a changed `requirements-runtime.txt`
is not applied to the reused `venv`. Nothing is carried forward on a first
activation with no earlier `current/venv`, which is why step 4 still checks
first and why `$PY` is pinned to the old release (the systemd unit would fail
at `ExecStartPre` without it). If step 4 prints
`venv MISSING`, do **not** start the service. Roll back (next section; the
previous release is retained and the pinned `$PY` still works) and report the
defect to the maintainers. The only code that builds a venv is the installer
(`packaging/install_sonder.sh`: `python3 -m venv`, then
`pip install -r requirements-runtime.txt`); repeating that for an updated
release has not been tried here.

**Open item (2026-11-20, code read only, not exercised): no supported
identity is proven for `install` and `rollback`.** Root fits the root-owned
`/opt/sonder` layout, but nothing in the repository documents or tests
`update install` run as root against the packaged install. Two consequences
are unverified. Files that root creates in the state home (`updates.db` and
its journal files, the pre-update backup) are owned by root, and the `sonder` service user may then be unable to open them. And the
alternative, giving `sonder` write access to `/opt/sonder`, `releases/` and
`current`, would break the root-owned immutable-release layout that
[install-server-private](install-server-private.md) documents, so it is not
recommended here. After an `update` run, compare `ls -ln /var/lib/sonder` and
`/var/lib/sonder/backups` (the pre-update backup directory) with the `sonder` user's uid before starting the service,
and report what you find to the maintainers.

**Where the pre-update backup goes.** The CLI install writes it to
`<state home>/backups`, the `backups` directory beside operations.db
(`self._backup_target or Path(store_db_paths()["operations"]).parent / "backups"`
in `sonder_runtime/adapters/updates/engine.py`). `cmd_update` builds
`UpdateManager()` without a `backup_target`, and no caller passes one, so
`[backup].target` is not used: on the packaged layout the backup is in
`/var/lib/sonder/backups`, not `/var/backups/sonder`. `backup list --config
/etc/sonder/sonder.toml` reads `[backup].target` and so will not show it; pass
`--target /var/lib/sonder/backups` to list it.

## Rollback

Use the same root `$UPD` prefix, pinned to the pre-upgrade release's
interpreter and run from the same working directory (`$OLD_RELEASE`; `cd` back
into it in a new shell, and redefine `$PY` and `$UPD` first):

```bash
$UPD status                       # note the previous release id
sudo systemctl stop sonder
$UPD rollback --confirm <last-8-of-previous-release-id>
sudo systemctl start sonder
```

`rollback` prints the restored release and then "Rollback complete. Restart
the service to run the restored release."; it exits 0 on success and 1 with
`update failed: ...` when refused.

Code rollback is refused when the previous release directory is missing —
that case requires state restore from the pre-update backup
([backup-restore](backup-restore.md)), which the install created and recorded in the
update journal (`backup_id` on the plan).

Before a planned release change, the local-only offline recovery rehearsal in
[backup-restore](backup-restore.md#offline-recovery-rehearsal) can exercise that relationship on a disposable copy.  It
checks the backup manifest and checksum index against an expected source
revision, restores every authoritative store, simulates a candidate upgrade,
rolls the candidate back after the expected failure, restores the state, and
verifies bounded cleanup.  The rehearsal never activates a release pointer,
stops a live service, or claims coordinator takeover; live failover and
provider behavior remain separate operational concerns.

## Failure triage

- `update status` shows the plan state and error code.
- Step-level evidence lives in updates.db `update_step`.
- `blocked` = compatibility refused before any change; nothing to undo.
- `rolled_back` = activation failed after staging; previous release still
  active; investigate the retained failed release directory.
- `failed` at `backing_up` = nothing was installed; fix backups first.
