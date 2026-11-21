# Start, stop, and drain

## Working directory for the `python -m sonder_runtime` commands

The commands on this page run `python -m sonder_runtime ...` and must be run
**from `/opt/sonder/current`** (the release directory). The release's virtual
environment holds only the third-party requirements and nothing installs
`sonder_runtime` into it, so the module resolves only when the working
directory contains it; the systemd unit sets
`WorkingDirectory=/opt/sonder/current` for the same reason
(`packaging/systemd/sonder.service`). From any other directory the command
prints `No module named sonder_runtime` and exits 1. Run `cd /opt/sonder/current`
once in your shell before the commands below.

## Start

```bash
cd /opt/sonder/current
sudo systemctl start sonder
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime status \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --json
```

Pass `--config`: without it `status` uses `SONDER_CONFIG` when that variable
is set and otherwise reads only `<default home>/sonder.toml`, and reports the
schemas of an unrelated home.

The unit's `ExecStartPre` runs preflight; a failing preflight prevents
the listener from ever opening. Investigate with:

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime preflight \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --json
```

## Stop (graceful)

```bash
sudo systemctl stop sonder
```

`systemctl stop` delivers SIGTERM. The runtime then:

1. moves to DRAINING and rejects new mutating work,
2. cancels non-durable foreground requests,
3. lets active durable task steps reach a safe checkpoint,
4. marks unfinished ownership as interrupted at the deadline,
5. flushes logs and operation events, closes databases, exits.

The drain deadline (25 s, not configurable) is below the unit's
`TimeoutStopSec=35`, so a healthy drain is never SIGKILLed.

## Drain (request a graceful shutdown over HTTP)

`drain` asks a running server to drain and then **exit**. It is not a
"pause": once the drain finishes, the server's flush hook shuts the HTTP
listener down and the process ends (checked 2026-11-20 against
`sonder_runtime/interfaces/http/serve.py`, `_handle_admin_drain` and the
flush hook registered next to `httpd`). Use it when you want the same
graceful sequence as `systemctl stop` without sending a signal, for example
from a remote shell or a script.

```bash
sudo -u sonder /opt/sonder/current/venv/bin/python -m sonder_runtime drain \
    --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env --json
```

Expected: JSON containing `"draining": true` and a `correlation_id`, then
the server stops accepting connections after the drain.

- Pass the server's `--config`. The unit starts `serve` with `--config` only
  and supplies the secrets through `EnvironmentFile=/etc/sonder/sonder.env`
  (`packaging/systemd/sonder.service`), so the CLI needs `--secrets` pointing
  at that file to see the API key. The command posts to `http://127.0.0.1:<[server].port>/v1/admin/drain` with the
  configured API key as a bearer token, so the port and key come from that
  configuration. It always targets `127.0.0.1` (`cmd_drain` in
  `sonder_runtime/__main__.py`), so it cannot reach a server whose
  `[server].host` is a specific non-loopback address: that prints
  `cannot reach the runtime on port <port>` and exits 1 (stop such a server
  with `systemctl stop`).
- The endpoint is administrator-only and answers `202 Accepted`. The CLI
  prints `drain request rejected: HTTP <code>` and exits 1 on any HTTP error:
  - `401` for a missing or wrong key (not in `local-open` mode, where drain
    needs no key: `_admin_authorized` in `serve.py`). An auth rate limit can
    refuse the request before the key is checked (`_auth_rate_limited`).
  - `403` for an authenticated caller that is not an administrator. In auth
    mode `both` the CLI sends only `Authorization: Bearer <api_key>` and no
    `X-Sonder-Account-Token`, so the request is not authorized at all and the
    CLI drain gets `401`, not `403`, before `_admin_authorized` in `serve.py`
    is reached; use `systemctl stop`, or call `POST /v1/admin/drain` with the
    API key and an administrator account's `X-Sonder-Account-Token`.
  - An unreachable server prints `cannot reach the runtime on port <port>`
    and exits 1.
- A configuration error prints the error and exits 2 before any request is
  sent. On the packaged install `install_sonder.sh` sets
  `/etc/sonder/sonder.env` to `sonder:sonder` with mode 0600 on every run, so
  `secrets file unreadable` means the file was created or edited by hand with
  root ownership (or with group/world access, which the loader also rejects).
  Restore it with `sudo chown sonder:sonder /etc/sonder/sonder.env && sudo chmod
  0600 /etc/sonder/sonder.env`; see
  [install-server-private](install-server-private.md#3-configuration-and-secrets).
- The drain deadline is 25 s and is not configurable.
- Under systemd the unit has `Restart=on-failure`. A drain is a clean
  shutdown, so systemd does not restart the service; start it again with
  `sudo systemctl start sonder` (check `systemctl status sonder` if the
  service comes back unexpectedly, the exit status was not checked here).

## Workstation (headless supervisor)

A workstation install (no systemd) is run by the headless supervisor
`sonder-headless.cmd` (Windows) or `sonder-headless.sh` (Linux/macOS). The
commands, the `--host`/`--port` defaults, where the PID and log files live, the
start wait and the exact stop behaviour (including "unmanaged listener") are
documented once, in
[CLI reference: sonder_headless.py](../wiki/04-cli-and-entrypoint.md#sonder_headlesspy-sonder-headless).
This section keeps only the procedure.

```powershell
# Windows (from the repository checkout or engine bundle directory)
.\sonder-headless.cmd start
.\sonder-headless.cmd status
.\sonder-headless.cmd stop
```

```bash
# Linux / macOS (from the checkout)
./sonder-headless.sh start
./sonder-headless.sh status
./sonder-headless.sh stop
```

- After `start`, check `status`. If `start` reports that the server is not
  reachable yet, read the server log file that `status` prints.
- **Windows `stop` has no graceful drain.** It force-ends the process tree.
  If in-flight work matters, drain first: run `drain` (below), wait for the
  server to exit, then run `stop`. POSIX `stop` sends SIGTERM and so runs the
  normal graceful drain; if it reports the server still running, wait and
  check again rather than killing it.
- To drain a workstation server, run `drain` with the same interpreter the
  launcher chose (`SONDER_PYTHON`, else the bundled Python or the checkout's
  `venv`; see
  [How the runtime is chosen](../wiki/04-cli-and-entrypoint.md#how-the-runtime-is-chosen-sonder-runtime))
  and from the checkout or bundle directory, for the module-path reason at the
  top of this page:

  ```powershell
  cd <checkout-or-bundle-dir>
  & <python-interpreter> -m sonder_runtime drain --json
  ```

  `drain` takes its port from `[server].port` in the configuration, not from
  the supervisor's `--port`. If you started the supervisor with a non-default
  `--port`, either export the same `SONDER_PORT` in the shell that runs drain
  (configuration loading honours it from the process environment,
  `sonder_runtime/platform/config.py`) or add `--set server.port=<port>` to
  the drain command; otherwise it contacts the default port and prints `cannot reach the
  runtime on port <port>`.

## Emergency stop

Only if a graceful stop hangs past its deadline:

```bash
sudo systemctl kill -s SIGKILL sonder
```

Afterwards check for interrupted durable work before restarting:
interrupted Autopilot/fleet tasks stay explicit and are never silently
replayed.
