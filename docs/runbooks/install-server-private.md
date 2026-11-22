# Install: server-private profile

Target: self-hosted Linux server, single private owner, systemd.
Sonder binds loopback only; remote access goes through a TLS reverse proxy
([Secure remote access](secure-remote-access.md)). Facts below were checked
against the code on 2026-11-20 by reading it; the installer was not run on a
Linux host for this revision.

Prerequisites: Linux with systemd, `python3` with `venv`, root (`sudo`), a git
checkout of a reviewed tag or commit, internet access to PyPI for the
dependency install, and Ollama (installed separately, see
[Provision models](#5-provision-models)).

## Directory layout

| Path | Owner / mode | Holds |
|---|---|---|
| `/opt/sonder/releases/<tag>/` | root | Immutable release and its own `venv/` |
| `/opt/sonder/current` | root | Symlink to the active release |
| `/etc/sonder/` | root, 0755 | `sonder.toml` (0644, no secrets), `sonder.env` (secrets) |
| `/var/lib/sonder/` | `sonder`, 0700 | State home (databases); also the `sonder` user's home |
| `/var/log/sonder/` | `sonder`, 0700 | Logs |
| `/var/backups/sonder/` | `sonder`, 0700 | Backups (`[backup].target`; not encrypted) |
| `/srv/sonder/workspaces/` | `sonder`, 0750 | The default `[state].workspace_roots` entry |

Source: `packaging/install_sonder.sh:65,84-87,176`.

## 1. Build the audited package

```bash
git clone https://github.com/Krilliac/Sonder-runtime.git
cd Sonder-runtime
git checkout <reviewed-tag-or-sha>
python3 scripts/package_local_system.py --out dist/local-system
```

Build as the unprivileged checkout owner. The packager includes only tracked,
allowlisted runtime files, privacy-scans them, and writes their sizes and
SHA-256 hashes to `PACKAGE-MANIFEST.json`.

## 2. Install the verified release

This runbook covers the initial source-checkout install. Install from a reviewed
tag or exact commit through the manifest-verified package path — never copy or
run production from a mutable developer checkout. SPEC-4 signed bundles are
the separate update/distribution path described in `publish-release.md`.

```bash
VERSION_TAG=$(git rev-parse --verify HEAD)
sudo packaging/install_sonder.sh \
  --package-source dist/local-system \
  --version-tag "$VERSION_TAG"
```

The installer validates the version tag before constructing root-owned paths,
verifies every manifest entry before copying it, ignores every unlisted file,
and refuses to overwrite an existing release. The release is built at its final
path and removed again if any step fails before the preflight passes;
`/opt/sonder/current` and the systemd units are only changed after it does. The
manifest verifier it runs is the one shipped beside `install_sonder.sh`
(started with `python3 -I`), never code from `--package-source`, so a
tampered package cannot execute anything as root before it is checked.

What the installer does, in order (`packaging/install_sonder.sh`, `set -euo
pipefail` at `:16`):

1. Creates the `sonder` system user and the directories above (`:84-87`).
2. Creates `/opt/sonder/releases/<tag>` (the release is built at its final
   path, because a virtualenv bakes its own location into its scripts) and
   registers an `EXIT` trap that removes it again on any failure before the
   release is committed (`:65-80,94-95`).
3. Copies the manifest-verified payload into it, builds the release
   virtualenv, runs `pip check` and writes `INSTALLED-REQUIREMENTS.txt`
   (`:126-136`).
4. Writes `/etc/sonder/sonder.toml` from `packaging/sonder.toml.example` if it
   is absent (`:139-142`), generates `/etc/sonder/sonder.env` if it is absent
   (`:143-153`) and sets it to `sonder:sonder`, mode 0600, on every run
   (`:160-161`).
5. Runs `preflight --skip-ollama` as `sonder` from the release directory
   (`:169-173`). A failure prints `preflight reported problems` and exits 1;
   the previous release, if any, stays current.
6. Only then flips `/opt/sonder/current` and marks the release committed
   (`:176-177`), copies the five systemd units and runs
   `systemctl daemon-reload` (`:178-183`).

It never starts or enables anything.

**Known gap (checked 2026-11-21 by reading the code and the packager's own
filter, not by a Linux install): on a fresh host the installer stops at item 4
of the list above until you work around it.** The payload is limited to the
manifest, which is filtered by `_included()` in
`scripts/package_local_system.py:350`. `packaging` is not in `ALLOWED_DIRS`
(`:24-45`) and `.example`, `.service` and `.timer` are not in
`ALLOWED_SUFFIXES` (`:46-57`), so the release contains none of
`packaging/sonder.toml.example`, `packaging/sonder.env.example` or
`packaging/systemd/*`. The script reads them from `$RELEASE_DIR/packaging/`
(`install_sonder.sh:140,178-182`), so without a pre-existing
`/etc/sonder/sonder.toml` it dies at the `sonder.toml` copy with:

```text
install: cannot stat '/opt/sonder/releases/<tag>/packaging/sonder.toml.example': No such file or directory
```

The `EXIT` trap then prints `install failed; removing incomplete release` and
deletes the half-built release, so no release directory or `current` link is
left behind (the `sonder` user and the directories from item 1 remain, which is
harmless) and a rerun, with the same or a new tag, is fine.

Workaround (**derived from the script, not yet run on a host**): install
`/etc/sonder/sonder.toml` from the checkout before running the installer, run
the installer, and then install the units by hand. With `sonder.toml` present
the installer skips the copy, generates `sonder.env`, passes the preflight and
flips `current`; it then stops at the first unit copy (`:178`) with the same
`cannot stat` error, and because the release is already committed the trap
leaves it in place.

```bash
cd Sonder-runtime            # the checkout from step 1
# Before the installer: the configuration (skip if /etc/sonder/sonder.toml exists and is yours)
sudo install -d -m 0755 /etc/sonder
sudo install -m 0644 packaging/sonder.toml.example /etc/sonder/sonder.toml
# Now run the installer as above. It is expected to stop at the first unit copy.
# After it: the five units, then reload
for u in sonder.service sonder-backup.service sonder-backup.timer \
         sonder-restore-smoke.service sonder-restore-smoke.timer; do
  sudo install -m 0644 "packaging/systemd/$u" /etc/systemd/system/
done
sudo systemctl daemon-reload
```

Then continue at step 3 (review the configuration); the installer has already
passed the preflight, and step 4 re-runs it. Every later install of a new tag
stops at the same first unit copy, after the new release is already current:
reinstall the units from the checkout if they changed. When the packager
allowlist is fixed this section will be replaced by the installer's own
behaviour.

Other failure modes:

- `release ... already exists; refusing to overwrite in place`: pick a new
  `--version-tag`.
- `preflight reported problems` (installer exit 1, `:169-173`): fix what it
  names and rerun; the release is removed and the previous one stays current.
  The installer turns any non-zero preflight exit into 1; step 4 lists what the
  preflight itself returns.

### Dependency resolution (known gap)

`requirements-runtime.txt` pins only the top-level runtime releases
(`mcp`, `cryptography`, `pywin32` on Windows). Their transitive dependencies
are resolved from PyPI at install time, without hashes, so two installs of
the same tag can differ and may not match the set CI tested. There is no
lock generator in the repository yet, so no hashed lock is shipped. Until
one exists the installer:

- runs `pip check` and aborts before the release becomes current if the
  resolved set is inconsistent, and
- writes the exact resolved set to
  `/opt/sonder/releases/<tag>/INSTALLED-REQUIREMENTS.txt`.

Compare that file between installs (or against the CI run for the same
revision) before trusting a new host; install from an internal mirror or
wheelhouse if you need a byte-identical closure. (Re-checked 2026-11-20:
`install_sonder.sh:127-136`.)

## 3. Configuration and secrets

The installer (with the `sonder.toml` workaround from step 2) leaves the
default loopback configuration in `/etc/sonder/sonder.toml` and generates
`/etc/sonder/sonder.env` with `SONDER_API_KEY` and `SONDER_AUTH_SECRET` (mode
0600, never printed; `install_sonder.sh:139-153`). Read the API key from that
file when configuring a client. Edit `/etc/sonder/sonder.toml` for the host.

Checklist before first start:

1. **Secrets ownership.** The installer creates `sonder.env` owned by
   `sonder:sonder`, mode 0600, and repairs an older root-owned file on every run
   (`install_sonder.sh:154-161`). Keep it that way: the loader rejects any
   group or world permission bit (so 0640 `root:sonder` is not a fix), and any
   command you run as `sonder` with `--secrets /etc/sonder/sonder.env` needs to
   read it. systemd reads it as root (`EnvironmentFile=`) regardless of owner.
   If you created or replaced the file yourself, restore it:

   ```bash
   sudo chown sonder:sonder /etc/sonder/sonder.env
   sudo chmod 0600 /etc/sonder/sonder.env
   ```

   The config loader checks only readability and mode, not ownership
   (`sonder_runtime/platform/config.py:2514-2540`). Trade-off: the service user
   owns its secrets file, but `ProtectSystem=strict`
   (`packaging/systemd/sonder.service:31`) keeps `/etc` read-only to the
   service process, so it cannot rewrite them at runtime.
2. **API key length.** This profile requires `SONDER_API_KEY` of at least 24
   characters (`config.py:128`, `:1990-1995`). The generated key is 43. To make
   your own: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. **Workspace roots.** Every `[state].workspace_roots` entry must exist and be
   writable by `sonder`, or preflight fails with
   `workspace_root:<path>` (`sonder_runtime/adapters/preflight.py:23-47`). The
   default `/srv/sonder/workspaces` is created by the installer; if you add
   roots, `sudo install -d -o sonder -g sonder -m 0750 <path>`.
4. **Optional first admin.** To create the first account over the network, add
   `SONDER_BOOTSTRAP_SECRET` (at least 16 characters, one-shot) to `sonder.env`;
   see the comments in `packaging/sonder.env.example` and
   (`admin_auth.py:19,75-76,216-229`). It works here only because systemd loads
   the file into the service environment.
5. **Provider variables** (`SONDER_MODEL_BACKEND`, per-tier
   `SONDER_*_PROVIDER`, ...) are read from the process environment, so put them
   in `sonder.env`, not `sonder.toml` (`adapters/provider_bindings.py:174`).

## 4. Validate before first start

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime preflight \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --skip-ollama
```

Expect exit 0. Not every check shows ok: on a fresh state home the
`schema:<store>` checks report `pending migrations` with ok false but not
required, which does not fail the run (`adapters/preflight.py:80-83`). A config
error such as `secrets file unreadable` makes the preflight exit 2
(`sonder_runtime/__main__.py:392-396`); a failed required check exits 1
(`:400`). Only `install_sonder.sh` collapses both into its own exit 1.
`--skip-ollama` is correct until you
have installed Ollama (next step); the service runs the full preflight (with
the Ollama probe) itself before it starts (`packaging/systemd/sonder.service:21`).

A fresh state home is stamped at epoch 2 by `serve`, `migrate`, `repl` and
`mcp` before they create any store, so running `migrate` first is safe
(`_stamp_home_before_stores`, `sonder_runtime/__main__.py:1180-1206`; `serve`
stamps inline at `:1276-1283`). Only `smoke` skips the stamp: it creates the
operations store on an unstamped home and the next `serve` then refuses it with
`migration required before serve ... run migrate --adopt-epoch2` (`:1287`). Do
not run `smoke` on a fresh home before the first `serve`; if you did, follow
[the repair](install-workstation-local.md#fresh-state-home-and-the-epoch-2-gate).

## 5. Provision models

Sonder needs a local Ollama and a base model aliased to `sonder:latest`.

1. Install and start Ollama from its own documentation (<https://ollama.com>);
   Sonder does not install it. The unit orders itself after `ollama.service`
   (`After=` in `packaging/systemd/sonder.service`).
2. Unless you deliberately consent to a remote Ollama, it must be the loopback
   one: `OLLAMA_HOST` unset or `127.0.0.1:11434`. A non-loopback `OLLAMA_HOST`
   is blocked without remote consent (`SONDER_ALLOW_REMOTE_OLLAMA`,
   `domain/ollama_policy.py:17`; applied in
   `adapters/inference/ollama_endpoint.py:206-216`) and `setup_alias.py` then
   exits 4. `sudo` normally resets the environment, so a shell-level
   `OLLAMA_HOST` rarely reaches `sudo -u sonder` anyway. For a remote worker
   see [Remote Ollama behind Caddy](remote-ollama-caddy.md).
3. Pull the base model and create the alias, from the release as the
   `sonder` user:

   ```bash
   cd /opt/sonder/current
   sudo -u sonder /opt/sonder/current/venv/bin/python /opt/sonder/current/setup_alias.py
   ```

   `setup_alias.py` is part of the release package
   (`scripts/package_local_system.py:169`). It picks a `qwen2.5-coder` size from
   the host's live RAM unless you pass `--model <name>` (or set
   `SONDER_BASE_MODEL`), pulls the embedding model (`--embed-model`, env
   `SONDER_EMBED_MODEL`, default `nomic-embed-text`; `--no-embedding` skips
   it), and creates the alias `sonder:latest`. Other flags: `--ollama <exe>`,
   `--offline` (use only models already present), `--gguf <file>` and
   `--from-usb` / `--usb-root <dir>` (import a local GGUF)
   (`setup_alias.py:198-235`). Exit codes: 4 endpoint blocked, 2 failed base
   pull or no GGUF found, 3 alias creation failed
   (`setup_alias.py:243,286,307`).
4. Check: `ollama list` shows `sonder:latest`. Then run the full preflight
   (without `--skip-ollama`) as in step 4; it must now pass.

Do not use `deploy_sonder.sh` for this profile: it is the separate
alias-and-loopback-service script (it writes `/etc/sonder/sonder-local.env`, not
the installer's layout). Its Ollama-endpoint import was repaired on main
(`deploy_sonder.sh:29`) and it no longer prints the API key, but this runbook's
layout, units and checks apply only to `install_sonder.sh`.

## 6. Enable the service (first start)

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sonder
sudo systemctl status sonder --no-pager
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime status --json \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env
```

The units must already be in `/etc/systemd/system` (the installer, or step 2's
manual unit copy); `daemon-reload` picks up any change. Expect
`active (running)` and a `status` document with no `config_errors`. If the
service fails, `journalctl -u sonder -n 50` shows the preflight or `serve`
message (a failed preflight blocks the listener). Check `/live` locally:
`curl -s http://127.0.0.1:11435/live` (`[server].port` default 11435).

## 7. Enable backup timers

```bash
sudo systemctl enable --now sonder-backup.timer sonder-restore-smoke.timer
```

Verify with `systemctl list-timers 'sonder-*'`. The backup unit runs daily with
`Persistent=true`. Create, verify, prune and restore commands, and how to
monitor backup health, are in [Backup and restore](backup-restore.md).
Backups are not encrypted: keep `/var/backups/sonder` mode 0700.

## 8. Remote access (optional)

Never expose the runtime port directly; keep `[server].host = "127.0.0.1"`.
Put a TLS reverse proxy (nginx or Caddy, with real certificates) in front,
following [Secure remote access](secure-remote-access.md); references are in
`packaging/reverse-proxy/`. Behind a proxy keep
`tls_terminated_by_proxy = true` and list the proxy's address in
`trusted_proxy_cidrs` so per-client rate limits see the real client address.

## 9. Host names and `421 HOST_NOT_ALLOWED`

Operator step: only a `local-open` listener (no API key, no accounts) refuses
a non-local `Host` name with `421`. This profile has an API key, so any
well-formed host name is accepted; unspecified IP literals (`0.0.0.0`,
`[::ffff:0.0.0.0]`) are still refused
(`sonder_runtime/interfaces/http/host_policy.py:203-232`).
If you ever run local-open behind a proxy that keeps the public `Host`, add the
name to `[server].allowed_hosts` (max 64) or `SONDER_ALLOWED_HOSTS`. The
rules, the response body and log line are in
[HTTP API & lifecycle](../wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed).

## 10. Uninstall

```bash
sudo packaging/uninstall_sonder.sh                       # keeps state
sudo packaging/uninstall_sonder.sh --purge-private-state # destroys state too
```

Run it from the checkout (the script lives in `packaging/`). By default it
disables and removes the five units and deletes `/opt/sonder`, and preserves
`/var/lib/sonder`, `/var/backups/sonder`, `/etc/sonder` and `/var/log/sonder`.
`--purge-private-state` additionally deletes all four of those and the `sonder`
user: databases, backups, secrets and logs are gone, with no prompt
(`packaging/uninstall_sonder.sh:11-37`). Take a backup copy elsewhere first. The
workspace directory `/srv/sonder/workspaces` is never removed.

## 11. Upgrades and rollback

Use the update procedure in [Upgrade and rollback](upgrade-rollback.md),
including its `update rollback --confirm` flow ([Rollback](upgrade-rollback.md#rollback)).
Only if you upgraded by running `install_sonder.sh` with a new `--version-tag`
(not through `update`) is the manual route available: repoint the symlink
(`sudo ln -sfn /opt/sonder/releases/<old-tag> /opt/sonder/current`, then
`sudo systemctl restart sonder`). The older release refuses a state home whose
schema the newer one advanced (preflight reports `future schema`,
`sonder_runtime/adapters/preflight.py:72-74`), so restore the pre-upgrade backup
as well ([Backup and restore](backup-restore.md)).
