# Upgrade and rollback

Updates install from signed bundles into versioned release directories
and activate by an atomic pointer switch. The active release is never
modified in place, and the previous release is retained.

## Build a bundle (publisher side)

```bash
python -m sonder_runtime update build /path/to/checkout /path/to/out \
    --bundle-version 1.4.0 --channel stable
```

Produces `sonder-engine-<version>.tar.gz` and `manifest.json` (lengths,
SHA-256 per file, platform/arch, schema targets, health checks). For a
production channel the bundle directory must also carry TUF `metadata/`
signed through the threshold ceremony — an unsigned bundle is refused
unless explicitly allowed for development.

## Upgrade (operator side)

```bash
# 1. Import and verify (trust, hashes, compatibility):
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime update import \
    /media/sonder-offline-update --channel stable
# -> reports update_id, status=available, confirm_nonce

# 2. Review what will happen:
python -m sonder_runtime update status

# 3. Stop the service (drain-aware) and install with explicit confirmation:
sudo systemctl stop sonder
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime update install \
    <update-id> --confirm <nonce>

# 4. Restart on the new release and verify:
sudo systemctl start sonder
curl -s http://127.0.0.1:11435/version
python -m sonder_runtime update status
```

The install runs, in order: trust revalidation, staged extraction with
manifest verification, compatibility preflight, **verified backup**,
drain, atomic staging publish, migrations executed by the *target*
release, manifest health checks, atomic `current` switch, commit. Every
step is journaled in updates.db; a migration or health failure leaves
the previous release active and the failed release retained as evidence.

## Rollback

```bash
python -m sonder_runtime update status        # note previous release id
sudo systemctl stop sonder
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime update rollback \
    --confirm <last-8-of-previous-release-id>
sudo systemctl start sonder
```

Code rollback is refused when the previous release directory is missing —
that case requires state restore from the pre-update backup
(backup-restore.md), which the install created and recorded in the
update journal (`backup_id` on the plan).

## Failure triage

- `update status` shows the plan state and error code.
- Step-level evidence lives in updates.db `update_step`.
- `blocked` = compatibility refused before any change; nothing to undo.
- `rolled_back` = activation failed after staging; previous release still
  active; investigate the retained failed release directory.
- `failed` at `backing_up` = nothing was installed; fix backups first.
