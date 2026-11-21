# Connect clients to Sonder

One page for every way to talk to a Sonder Runtime: MCP in an agent, the
console, the thin client, the phone/desktop app, the browser web app, plain
HTTP, and the in-process SDK. Facts below were checked against the code on
2026-11-20. This is a setup page, so it deliberately does not use the runbook
Symptoms/Diagnosis/Recovery/Aftermath shape; failure modes sit beside each step.

## Preconditions

- Sonder is installed and its Python environment exists (see
  [install-workstation-local.md](install-workstation-local.md) for a local
  install or [install-server-private.md](install-server-private.md) for a
  server). Below, `<checkout>` is your clone and "the venv interpreter" is
  `<checkout>/venv/bin/python` (Linux/macOS/WSL) or
  `<checkout>\venv\Scripts\python.exe` (Windows). Do not use a system Python.
- For the thin client, the app, the web app and direct HTTP, the Sonder HTTP
  server must already be running (default port 11435, see
  [start-stop-drain.md](start-stop-drain.md)). MCP clients start their own stdio
  process, the REPL runs in-process, and the SDK is a library inside a host
  process, so none of those need the HTTP server.
- Ollama (default port 11434) must be reachable by whichever process serves
  the models; the launcher uses 11436 and Sonder-Inference 11437.

## Which client talks to what

| Client | Transport | Port | Auth | Home doc |
|---|---|---|---|---|
| MCP in Claude Code, Codex, Claude Desktop | stdio child process | none | none (process is yours) | this page; [integrations/](../../integrations/README.md) |
| Console (REPL), local | in-process | none | none | [wiki 04](../wiki/04-cli-and-entrypoint.md) |
| Thin client `sonder_client.py` | HTTP(S) to a server | server 11435 (or your proxy) | Bearer key | [CLIENT.md](../../CLIENT.md) |
| Phone / desktop app | HTTP(S) to a server, launcher on 11436 | 11435 / 11436 | API key, launcher token, optional account login | [app/README.md](../../app/README.md) |
| Browser web app | HTTP(S), served by the server at `/app/` | 11435 | API key / account login in the app | [wiki 03](../wiki/03-configuration.md) |
| Direct HTTP (UIs, scripts) | HTTP(S) | 11435 | `Authorization: Bearer` | [wiki 05](../wiki/05-http-api-and-lifecycle.md) |
| In-process SDK | Python import | none | host-owned | [developer-sdk.md](../developer-sdk.md) |

## MCP clients (stdio)

The supported entry point is `python -m sonder_runtime mcp`
(`sonder_runtime/__main__.py:2017`). It takes `--config`, `--secrets`,
`--set` and `--json` like the other subcommands. Configuration comes from
`--config` / `--secrets`, else `SONDER_CONFIG` / `SONDER_SECRETS`, else
`sonder.toml` / `sonder.env` in the state home if those files exist
(`__main__.py:59-87`). The transport is stdio only (`server.py:26807`,
`reloadable_mcp.py:849`); there is no HTTP/SSE MCP server mode.

**The checkout must be on the import path.** The repo has no `pyproject.toml`,
`setup.py` or `setup.cfg`, and the install runbooks only run
`pip install -r requirements-runtime.txt`, so `sonder_runtime` is never
installed into the venv. `python -m sonder_runtime` finds it only when the
current directory is the checkout. MCP clients start the server in their own
project directory, so every registration below sets `PYTHONPATH=<checkout>`
in the server's environment. Without it the server fails at start with
`No module named sonder_runtime`.

The server reports itself as `sonder-runtime` (`server.py:4781`). The name you
register it under is your choice; this repo's docs use `sonder`. Clients show
tools prefixed with the registered name, for example
`mcp__sonder__<tool>` in Claude Code.

### Claude Code

```bash
claude mcp add sonder --scope user -e PYTHONPATH=<checkout> -- <checkout>/venv/bin/python -m sonder_runtime mcp
claude mcp list
```

Windows (PowerShell):

```powershell
claude mcp add sonder --scope user -e PYTHONPATH="<checkout>" -- "<checkout>\venv\Scripts\python.exe" -m sonder_runtime mcp
claude mcp list
```

Expect `sonder` in the list. Environment values (cloud, model pins)
are passed per client; see [integrations/claude/mcp-config.md](../../integrations/claude/mcp-config.md).

### Codex

Append the block in
[integrations/codex/config.toml.example](../../integrations/codex/config.toml.example)
to `~/.codex/config.toml`: a `[mcp_servers.sonder]` table with `command` (the
venv interpreter), `args = ['-m', 'sonder_runtime', 'mcp']`,
`startup_timeout_sec = 120.0`, and a `[mcp_servers.sonder.env]` table whose
`PYTHONPATH` is the checkout (required, see above). Write paths as TOML
literal strings in single quotes, as the example does. In a double-quoted TOML
string a backslash starts an escape, so `"D:\sonder\runtime"` makes Codex fail
to parse the whole `config.toml`; if you must use double quotes, use forward
slashes or doubled backslashes.

### Claude Desktop

Add the same command and arguments as an entry under `mcpServers` in the
Claude Desktop configuration file:

```json
{
  "mcpServers": {
    "sonder": {
      "command": "<checkout>/venv/bin/python",
      "args": ["-m", "sonder_runtime", "mcp"],
      "env": { "PYTHONPATH": "<checkout>" }
    }
  }
}
```

The location of that file and the exact schema are client-side knowledge and
were not verified against this repository; check Claude Desktop's own
documentation. On Windows write the interpreter and `PYTHONPATH` paths with
escaped backslashes.

### Options and other notes

- `--native` selects the application-owned bounded MCP transport. It is
  experimental, and it is the only transport that exposes the `compute_*`
  tools. `--progressive-tools` needs `--native`; see
  [wiki 04](../wiki/04-cli-and-entrypoint.md).
- The MCP package is pinned as `mcp==2.2.0` in `requirements-runtime.txt`.
- Legacy path: registering `server.py` directly still runs, but it reads the
  environment only, not `sonder.toml` / `sonder.env`. It exits 1 with
  `WorkerPoolUnavailable ... start with python -m sonder_runtime mcp`
  (`server.py:26796-26805`) when the membership binding check fails: the pool
  has configured remote workers, or the primary Ollama origin is not loopback
  (`bootstrap/legacy_root.py:118`; `ollama_pool.py:1088-1089`). A
  loopback-only worker list does not trigger it. Fix: change the registration
  to `-m sonder_runtime mcp`.

Failure symptoms: the client shows the server as failed or times out on first
start. Run the exact command by hand in a terminal with `PYTHONPATH` set (or
from the checkout); it should wait silently on stdin (stdio MCP).
`No module named sonder_runtime` means `PYTHONPATH` is missing from the
registration. Any other traceback names the missing dependency or config error.
First start can be slow, hence the generous Codex timeout.

Rollback: `claude mcp remove sonder`, or delete the TOML table / JSON entry.

## Console (REPL)

Local, on the machine that has Sonder. Run it from the checkout (or set
`PYTHONPATH=<checkout>`), for the same import reason as MCP:

```bash
cd <checkout> && venv/bin/python -m sonder_runtime repl
```

`repl --json` emits `sonder.repl-output.v1` JSON Lines; setting
`SONDER_REPL_NDJSON=1` on a piped session emits `sonder.repl-turn.v1` lines.
`sonder.repl-output.v1` is described in
[client-surface-contracts.md](client-surface-contracts.md); `sonder.repl-turn.v1`
is only in [wiki 04](../wiki/04-cli-and-entrypoint.md).

Remote: use the thin client, which needs only Python and a checkout (no
venv, no Ollama). Setup, flags and exit codes are in
[CLIENT.md](../../CLIENT.md). The essentials:

```bash
export SONDER_SERVER=https://sonder.example.internal
export SONDER_API_KEY=<key>
python3 sonder_client.py
```

The argv overrides, the HTTPS-or-loopback key rule and its exit codes, the
redirect rule, and the local fallback (`SONDER_LOCAL_FALLBACK`,
`SONDER_FALLBACK_LOCAL`, and when it is skipped) are owned by
[CLIENT.md](../../CLIENT.md). Prefer the environment variable over `--key`,
which is visible in process listings.

## Phone and desktop app

The Flutter app is installed and built per [app/README.md](../../app/README.md);
host-side control is in [MOBILE_HOST_CONTROL.md](../../MOBILE_HOST_CONTROL.md).
There is no QR code or pairing flow. In the app's Settings you enter the
server URL, the API key, the launcher URL (hint `https://your-host:11436`), the
launcher token, and optionally sign in to an account.

- The API key is sent over HTTPS, over loopback (including `localhost`), or to
  a host:port you explicitly allow as plain HTTP in Settings
  (`app/lib/api/transport.dart:480-540`).
- Account sign-in needs HTTPS or a numeric loopback address; `localhost` is not
  accepted (`app/lib/account_session.dart:22-40`).
- The app's local fallback is `http://127.0.0.1:11435` on native builds and
  empty on web builds (`kIsWeb ? '' : ...`, `app/lib/api.dart:910`).

Failure: for a plain-HTTP, non-loopback URL the app silently leaves the key
out (`app/lib/api/transport.dart:562-571`), so the server answers 401
`UNAUTHENTICATED` (`serve.py:5005-5012`). Settings shows "The API key is not
sent to <host>: this address uses unencrypted HTTP. Use HTTPS, or allow this
host." (`app/lib/settings/page_connection.dart:198-199`). Account sign-in
throws "Account traffic requires HTTPS or numeric loopback"
(`app/lib/account_session.dart:39`). Put the server behind TLS
([secure-remote-access.md](secure-remote-access.md)) and use the `https://` URL.

## Browser web app

The server can serve the app's web build at `/app/`. It is off by default:
set `[server].web_app_enabled = true` or `SONDER_WEB_APP=1`; the bundle
directory is `[server].web_app_dir` / `SONDER_WEB_APP_DIR` (must be an
absolute path, enforced at `platform/config.py:1971-1973`; the env variable is
read at `:1582`), default `<state home>/web_app` (`platform/config.py:2423-2425`). Build it with
`python scripts/build_web_app.py`. Full procedure and the security model are in
[wiki 03, "Serving the web app"](../wiki/03-configuration.md#serving-the-web-app).
Enabled without a bundle, `/app/` answers 404 `WEB_APP_NOT_BUILT`.

## Direct HTTP (OpenAI-compatible UIs and scripts)

- Base URL: `https://<your-host>` (or `http://127.0.0.1:11435` on the server
  itself). Chat endpoint: `POST /v1/chat/completions`.
- Authenticate with `Authorization: Bearer <key>`. Which paths are exempt,
  and the `local-open` mode where nothing is authenticated, are set out in the
  authentication paragraph of [wiki 05](../wiki/05-http-api-and-lifecycle.md)
  (before "Host names and 421"). Do not assume a remote call is exempt.
- Send the key only over `https://` (or loopback). Anything that logs the
  request line or headers on the way (proxies, UI debug logs) will see it.

```bash
curl -s https://sonder.example.internal/v1/models \
  -H "Authorization: Bearer $SONDER_API_KEY"
```

A 401 means a missing or wrong key. Endpoint list and lifecycle:
[wiki 05](../wiki/05-http-api-and-lifecycle.md). Key handling and rotation:
[configure-authentication.md](configure-authentication.md), [wiki 09](../wiki/09-security-model.md) and
[rotate-credentials.md](rotate-credentials.md).

## In-process SDK

`sonder_runtime/sdk.py` is a Python library for code running inside a Sonder
host process (`SDK_PROTOCOL_VERSION` `1.0`,
`interfaces/sdk/contracts.py:17`). `GatewayTransport` needs a host-composed
`ToolApplicationFacade` (`interfaces/sdk/client.py:39-60`), so it is not a
network client. Programs outside the process should use HTTP. See
[developer-sdk.md](../developer-sdk.md).

## Behind a proxy

When a reverse proxy (nginx, Caddy) fronts the server, point every client at
the proxy's `https://` URL. Do not rely on `http://` to `https://` redirects:
the thin client refuses to follow redirects when it carries a key, and apps
treat plain HTTP to a non-loopback host as insecure. Proxy setup, header
handling and verification are in [secure-remote-access.md](secure-remote-access.md).
