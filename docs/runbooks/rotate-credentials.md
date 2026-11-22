# Rotate credentials

Verified against the code on 2026-11-21. First-time authentication setup:
[configure-authentication](configure-authentication.md).

## API key (planned rotation)

Run the command as `root` with the service stopped, then restore `sonder:sonder`
ownership and mode `0600` on the files it writes. Do **not** run `rotate-key` as
`sonder` on a stock install (see [Why not `sudo -u sonder`](#why-not-sudo--u-sonder)).

```bash
sudo systemctl stop sonder
sudo /opt/sonder/current/venv/bin/python -m sonder_runtime rotate-key \
    --config /etc/sonder/sonder.toml \
    --secrets /etc/sonder/sonder.env --overlap-seconds 86400
sudo chown sonder:sonder /etc/sonder/sonder.env
sudo chmod 0600 /etc/sonder/sonder.env
sudo chown sonder:sonder /var/lib/sonder/secrets/rotation.json
sudo chmod 0600 /var/lib/sonder/secrets/rotation.json
sudo chown sonder:sonder /var/lib/sonder/operations.db*
sudo systemctl start sonder
```

Since #808, the installer creates `/etc/sonder/sonder.env` owned by
`sonder:sonder` mode `0600` (`packaging/install_sonder.sh:160-161`, repairing an
older root-owned file on every run) so both systemd (`EnvironmentFile`, read as
root) and `sudo -u sonder ... --secrets /etc/sonder/sonder.env` commands (such
as `preflight`) can read it while keeping group/world bits cleared. Running
`rotate-key` as `root` replaces `sonder.env` with a root-owned file, so the
`chown sonder:sonder` and `chmod 0600` lines restore the installed ownership.

The remaining `chown` lines cover `rotation.json` (which the `sonder` service
reads directly, mode `0600`) and `operations.db` plus any SQLite sidecar files
(`-wal`/`-shm`) created while recording the `API_KEY_ROTATED` audit event.
Adjust `/var/lib/sonder` if your state home differs; if the `secrets` directory
itself was created by `root`, chown it too
(`sudo chown sonder:sonder /var/lib/sonder/secrets`).

**Always pass `--config` (or set `SONDER_CONFIG`) on the packaged install.**
`rotate-key` writes the previous-key overlap record to
`<state home>/secrets/rotation.json`, and the state home comes from `--config`
(`sonder_runtime/__main__.py:60-88`, `:1681-1706`). The packaged `sonder.toml`
sets `[state].home = "/var/lib/sonder"` (`packaging/sonder.toml.example:30`).
Without `--config` the command uses the default home of the user running it
(`~/.local/share/sonder` or `$XDG_DATA_HOME/sonder`;
`sonder_runtime/platform/paths.py:170-194`), unless `SONDER_STATE_HOME` or
`SONDER_HOME` is set (`SONDER_STATE_HOME` wins over `SONDER_HOME`, matching
`sonder_runtime/platform/paths.py:157-167` and
`sonder_runtime/platform/config.py:1594-1599`; see
[configuration](../wiki/03-configuration.md)), and writes the overlap record
there instead of where the server looks. Alternatively set
`SONDER_ROTATION_STATE=<absolute path>` to name the file explicitly
(`sonder_runtime/adapters/secrets.py:35-39`); it must be the same path in the
server's environment.

What happens:

- A new `SONDER_API_KEY` (`token_urlsafe(32)`,
  `sonder_runtime/adapters/secrets.py:124-125`) is written into the secrets file
  (mode `0600`). Keys are never printed; read the new value from the file when
  updating clients. The file is replaced via a temporary file beside it
  (`sonder.env.tmp-<pid>`, `sonder_runtime/adapters/secrets.py:46-55`), so the
  running user needs write access to the secrets file's **directory**, not only
  to the file.
- The SHA-256 of the previous key is stored with a mandatory expiry (default
  24 h, minimum 60 s, maximum 7 days = 604800 s; other values are refused,
  `sonder_runtime/adapters/secrets.py:24-26`, `:136-139`). While the window is
  open the server accepts either key, checked on every request, so clients can
  be updated after the restart; the only outage is the restart window itself.
  After expiry only the new key works — no cleanup job required.
- The running server keeps the key it started with until restarted.
- Rotating again inside an open window **overwrites** `rotation.json`: the key
  from before the first rotation stops working immediately and only the key just
  replaced gets the new window (`sonder_runtime/adapters/secrets.py:178-192`).
- If the secrets file has no current `SONDER_API_KEY`, a key is added with no
  overlap state and the report shows `previous_accepted_until: null`
  (`sonder_runtime/adapters/secrets.py:179-200`).
- The rotation is recorded in `operations.db` as `API_KEY_ROTATED`.
- The state home also comes from `--set`/`SONDER_CONFIG` as for the server. The
  secrets file may also come from `SONDER_SECRETS` instead of `--secrets`. An
  invalid configuration, including a secrets file that is group/world
  accessible, is refused with exit 2 and nothing is rotated.

Verify: `previous_accepted_until` in the command output is a UTC time about
`--overlap-seconds` ahead, `<state home>/secrets/rotation.json` exists, and
after the restart both calls work until that time, then only the new key:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $OLD_KEY" \
    http://127.0.0.1:11435/v1/models
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $NEW_KEY" \
    http://127.0.0.1:11435/v1/models
```

Update each client with the new key before the overlap expires.

## API key (suspected compromise)

The shortest overlap window `rotate-key` allows is 60 seconds (there is no
zero-overlap option):

```bash
sudo systemctl stop sonder
sudo /opt/sonder/current/venv/bin/python -m sonder_runtime rotate-key \
    --config /etc/sonder/sonder.toml \
    --secrets /etc/sonder/sonder.env --overlap-seconds 60
sudo chown sonder:sonder /etc/sonder/sonder.env
sudo chmod 0600 /etc/sonder/sonder.env
sudo chown sonder:sonder /var/lib/sonder/secrets/rotation.json
sudo chmod 0600 /var/lib/sonder/secrets/rotation.json
sudo chown sonder:sonder /var/lib/sonder/operations.db*
sudo systemctl start sonder
```

While the service is stopped the old key is not accepted by anything; after the
start it is accepted only for what remains of the 60 s window. To cut off the
remaining window immediately after restart, remove
`<state home>/secrets/rotation.json` (`load_rotation_state` returns `None` when
the file is absent, `sonder_runtime/adapters/secrets.py:82-87`, and
`previous_key_valid` reads it per request, `:103-121`). Then follow
[suspected-secret-exposure](suspected-secret-exposure.md).

## Why not `sudo -u sonder`

Although `/etc/sonder/sonder.env` itself is owned by `sonder:sonder` (mode
`0600`, `packaging/install_sonder.sh:160-161`), the installer creates the
parent directory `/etc/sonder` as `root` mode `0755`
(`packaging/install_sonder.sh:87`). `rotate-key` atomically replaces
`sonder.env` by creating `sonder.env.tmp-<pid>` in `/etc/sonder`
(`sonder_runtime/adapters/secrets.py:46-55`), so running as `sonder` fails with
`PermissionError`, which `cmd_rotate_key` does not catch (it only handles
`RotationError`, `sonder_runtime/__main__.py:1707-1713`): the operator sees a
traceback and nothing is rotated.

Failure modes of getting ownership wrong:

- Run as `sonder` without changing `/etc/sonder`: `PermissionError` traceback as
  above; key unchanged.
- Run as `root` and skip `chown sonder:sonder /etc/sonder/sonder.env`:
  `sonder.env` becomes root-owned again, breaking `sudo -u sonder ... --secrets`
  commands (including `preflight`) even though systemd still starts the service.
- Run as `root` and skip `chown sonder:sonder` on `rotation.json`: the service
  cannot read `rotation.json` (mode `0600`), so `load_rotation_state` returns
  `None` on `OSError` (`sonder_runtime/adapters/secrets.py:82-87`) and the
  overlap window is silently not honoured (clients still on the old key get 401
  immediately).
- Run as `root` while the service is up: `root` may create root-owned SQLite
  sidecar files (`-wal`/`-shm`) next to `operations.db`, which the `sonder` user
  then cannot write. Stop the service first and keep the targeted `chown` lines.

Alternative if you want to run `rotate-key` as `sonder`: make `/etc/sonder`
writable by `sonder` once (`sudo chown sonder:sonder /etc/sonder`), noting that
this also lets the service account replace `/etc/sonder/sonder.toml`; the root
procedure above avoids that trade-off.

## Account auth secret

Rotating `SONDER_AUTH_SECRET` invalidates all account tokens. Edit
`/etc/sonder/sonder.env` (keeping ownership `sonder:sonder` and mode `0600`),
or replace `<state home>/secrets/auth_secret` when the variable is unset,
restart the service, and have users log in again. Do this during a maintenance
window unless responding to compromise.
