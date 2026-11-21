# Rotate credentials

Verified against the code on 2026-11-20. First-time authentication setup:
[configure-authentication](configure-authentication.md).

## API key (planned rotation)

Run the command as root with the service stopped, then hand the two files it
writes back to the service user. Do **not** run it as `sonder` on a stock
install (see [Why not `sudo -u sonder`](#why-not-sudo--u-sonder)).

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

The targeted `chown` lines cover `rotation.json` and operations.db plus any
SQLite sidecar file (`-wal`/`-shm`) the root-run command created next to it
(the audit event is written to operations.db). Adjust `/var/lib/sonder` if your
state home differs; if the `secrets` directory itself was created by root,
chown it too (`sudo chown sonder:sonder /var/lib/sonder/secrets`). The sidecar
rationale is inferred from the code, not observed on a host. The service itself reads `sonder.env`
through systemd as root, but it reads `rotation.json` as `sonder`, so that file
must belong to `sonder` (mode 0600).

**Always pass `--config` (or set `SONDER_CONFIG`) on the packaged install.**
`rotate-key` writes the previous-key overlap record to
`<state home>/secrets/rotation.json`, and the state home comes from `--config`
(`__main__.py:1633-1660`, `:74-87`). The packaged `sonder.toml` sets
`[state].home = "/var/lib/sonder"` (`packaging/sonder.toml.example:30`). Without
`--config` the command uses the default home of the user running it,
`~/.local/share/sonder` (or `$XDG_DATA_HOME/sonder`; `platform/paths.py:157-181`),
unless `SONDER_HOME` or `SONDER_STATE_HOME` is set, which override these
defaults (`rotate-key` loads the configuration, so `SONDER_HOME` wins, then
`SONDER_STATE_HOME`: `platform/config.py:1598-1600`, `__main__.py:1651-1658`;
see [wiki 03](../wiki/03-configuration.md#state-home)), and writes
the overlap record there. The server never reads that file, so the old key is not honoured
for the overlap and nothing warns you. Alternatively set
`SONDER_ROTATION_STATE=<absolute path>` to name the file explicitly
(`adapters/secrets.py:35-39`); it must be the same path in the server's
environment.

What happens:

- A new `SONDER_API_KEY` (`token_urlsafe(32)`, `adapters/secrets.py:124-125`)
  is written into the secrets file (mode 0600). Keys are never printed; read
  the new value from the file when updating clients. The file is replaced via a
  temporary file beside it (`sonder.env.tmp-<pid>`,
  `adapters/secrets.py:46-55`), so the running user needs write access to the
  secrets file's **directory**, not only to the file.
- The SHA-256 of the previous key is stored with a mandatory expiry (default
  24 h, minimum 60 s, maximum 7 days = 604800 s; other values are refused,
  `secrets.py:24-26`, `:136-139`). While the window is open the server accepts
  either key, checked on every request, so clients can be updated after the
  restart; the only outage is the restart window itself (the procedure above
  stops and starts the service). After expiry only the new key works; no cleanup job is needed.
- The running server keeps the key it started with. Until you restart it, the
  **old** key remains its current key (the new one is rejected). Restart to
  switch.
- Rotating again inside an open window **overwrites** `rotation.json`: the
  key from before the first rotation stops working immediately and only the
  key just replaced gets the new window (`secrets.py:178-192`).
- If the secrets file has no current `SONDER_API_KEY`, a key is added but no
  overlap state is written and the report shows `previous_accepted_until:
  null` (`secrets.py:179-199`).
- The rotation is recorded in operations.db as `API_KEY_ROTATED`.
- The state home also comes from `--set`/`SONDER_CONFIG` as for the server.
  The secrets file may come from `SONDER_SECRETS` instead of `--secrets`. An
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

The shortest window the tool allows is 60 seconds; there is no zero-overlap
option:

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

With the service stopped the old key is not accepted by anything; after the
start it is accepted only for what is left of the 60 s window. To end that too,
delete `<state home>/secrets/rotation.json` right after the start. This is an
**unsupported** shortcut, not a documented operation: it works only because
`load_rotation_state` returns no previous key when the file cannot be read
(`adapters/secrets.py:82-87`), and it is read per request
(`previous_key_valid`, `adapters/secrets.py:103-121`). Then follow
[suspected-secret-exposure](suspected-secret-exposure.md).

## Why not `sudo -u sonder`

The installer creates `/etc/sonder` as `root` mode 0755
(`packaging/install_sonder.sh:73`). `rotate-key` replaces `sonder.env` by
writing `sonder.env.tmp-<pid>` in that directory (`adapters/secrets.py:46-55`),
so as `sonder` it fails with `PermissionError`, which `cmd_rotate_key` does not
catch (it only handles `RotationError`, `__main__.py:1658-1663`): the operator
sees a traceback and nothing is rotated.
[install-server-private](install-server-private.md#3-configuration-and-secrets)
only makes the file readable by `sonder`; it does not cover this.

Failure modes of getting the ownership wrong:

- Run as `sonder` without changing `/etc/sonder`: `PermissionError` traceback
  as above, key unchanged.
- Run as root and skip the `chown`: `sonder.env` is root-owned again (undoing
  the install-server-private chown) and `rotation.json` is created root-owned
  mode 0600 under `<state home>/secrets`. The service cannot read it, and
  `load_rotation_state` returns no previous key on `OSError`
  (`adapters/secrets.py:82-87`), so the overlap is silently not honoured and clients
  still on the old key get 401 at once.
- Root run while the service is up: root may create root-owned database
  sidecar files (for example `-wal`/`-shm`) next to operations.db in the state
  home, which the `sonder` user then cannot write. Stop the service first and
  keep the targeted `chown` lines.

Alternative if you want to run as `sonder`: make the directory writable by that
user once (`sudo chown sonder:sonder /etc/sonder`), which also lets the service
account replace `sonder.toml`; the root procedure above avoids that
trade-off.

## Account auth secret

Rotating `SONDER_AUTH_SECRET` invalidates all account tokens. Edit the
secrets file, restart, and have users log in again. Do this during a
maintenance window unless responding to compromise.

The account secret is also read from `SONDER_AUTH_SECRET` in the secrets file,
or from `<state home>/secrets/auth_secret` when the variable is unset; replace
the value in whichever one is in use.
