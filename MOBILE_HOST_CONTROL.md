# Mobile host control for Sonder Runtime

> **Contract scope:** this focused contract describes current behavior. Unfinished
> implementation work is tracked in the
> [master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md); the
> [behavior status](#behavior-status) table labels what is implemented,
> experimental, proposed, degraded, or unsupported.

The Sonder Runtime Flutter app uses the same System page on desktop, Android,
iOS, and other client-only builds. A phone cannot create a process on a
powered-off or listener-free computer, so the host runs a small independent
launcher on port `11436`. The launcher can perform only four fixed operations:
status, start, stop, and restart of `sonder_headless.py`. It cannot accept a
command, executable path, shell argument, setup request, Git update, or
training request.

Sonder is the orchestration runtime, not the model being served. On the host,
Ollama stores and loads the configured base or deployed model weights and runs
local inference. `sonder_headless.py` supervises Ollama and the Sonder Runtime
API, where memory, tools, grounding, policy, training, and deployment are
orchestrated. The managed Sonder API remains loopback-only on port `11435`;
remote clients reach it through the documented TLS reverse proxy. Use a
different strong credential for each service:

- `SONDER_API_KEY` authenticates chat and System API requests.
- `SONDER_LAUNCHER_TOKEN` authenticates host process control.

Both must contain at least 24 characters for a remote endpoint. Distributed
mobile apps require HTTPS for both services.

A launcher bound to loopback without `SONDER_LAUNCHER_TOKEN` is still allowed
for single-user desktops, but it prints a `WARNING: ... WITHOUT
authentication` line on stderr at startup: any local process or user can then
start, stop, and restart Sonder through it. Set a token on shared machines.

The launcher checks the `Host` header with the same policy as the main
server, to stop DNS-rebinding pages in a local browser from driving it. The
main server's rules, and the `421 HOST_NOT_ALLOWED` answer, are described in
[HTTP API & lifecycle](docs/wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed).
The launcher has its own allowlist variable: any other name is accepted only
when a token is set or the name is listed in the comma-separated
`SONDER_LAUNCHER_ALLOWED_HOSTS`; otherwise the launcher answers
`421 HOST_NOT_ALLOWED` and its `remedy` names that variable.

## Launcher environment and flags

Checked against `sonder_launcher.py` on 2026-11-20. A flag, when it exists,
overrides the variable.

| Variable | Flag | Default | Meaning |
|---|---|---|---|
| `SONDER_LAUNCHER_HOST` | `--host` | `127.0.0.1` | Address the launcher listens on. Any non-loopback value needs a token and TLS (below). |
| `SONDER_LAUNCHER_PORT` | `--port` | `11436` | Launcher port. |
| `SONDER_LAUNCHER_TOKEN` | `--token` | empty | Bearer token for host control. At least 24 characters for a non-loopback bind. With no token, only loopback peers are accepted. |
| `SONDER_LAUNCHER_CERT`, `SONDER_LAUNCHER_KEY` | `--cert`, `--key` | empty | PEM certificate chain and private key. Both or neither; one alone is rejected at startup. TLS 1.2 or later. |
| `SONDER_HOST`, `SONDER_PORT` | `--server-host`, `--server-port` | `127.0.0.1`, `11435` | The managed Sonder API the launcher starts and probes. The host must be loopback, or the launcher refuses to start. |
| `SONDER_LAUNCHER_ALLOWED_HOSTS` | none | empty | Extra `Host` names to accept when no token is set. Comma-separated; a malformed entry is dropped with a message on stderr. |
| `SONDER_LAUNCHER_DB` | none | `<SONDER_HOME or the platform default>/run/sonder-launcher-operations.sqlite3` | Operation ledger path. For the standalone launcher this override always applies; `SONDER_HOME` only changes the default location. |
| `SONDER_LAUNCHER_COMMAND_JOURNAL` | none | `sonder-launcher-command-journal.jsonl` beside the ledger | Command-acknowledgement journal path. |
| `SONDER_LAUNCHER_START_TIMEOUT` | none | `1860` seconds (31 minutes) | Start and Restart time limit. Values are clamped to 1 second up to the default, so this can shorten the limit but not raise it. |
| `SONDER_LAUNCHER_STOP_TIMEOUT` | none | `60` seconds | Stop time limit, clamped the same way. |
| `SONDER_LAUNCHER_OPERATION_RETENTION` | none | `100` | Operations kept in the ledger; clamped to 1 through 1000. |
| `SONDER_LAUNCHER_QUIET` | none | unset | `1` silences the per-request log lines. |
| `SONDER_LAUNCHER_HEALTH_TOKEN` | none | generated | Optional. Secret the launcher uses to verify the Sonder server's identity. If set it must be at least 32 characters, and it must equal the persisted token when one already exists. Normally leave it unset: the launcher creates a private token file `sonder-launcher-health.token` beside the ledger. The launcher passes it to the managed Sonder server it starts; the runtime then strips it from the environments of tool and command child processes. |

Other flags: `--allow-insecure-http-for-development` (plaintext on a
non-loopback bind; see below) and `--generate-token` (print a random token and
exit).

Startup rules: a non-loopback `--host` without a token of 24 or more
characters is refused; a certificate without a key (or the reverse) is refused;
a non-loopback `--host` without a certificate is refused unless
`--allow-insecure-http-for-development` is given. A refusal prints
`ERROR: <reason>` on stderr and exits with status 2.

The launcher serves these routes (`GET /` is also answered as status): `GET /v1/launcher/status`,
`GET /v1/launcher/operations/<operation-id>`,
`POST /v1/launcher/start`, `POST /v1/launcher/stop`,
`POST /v1/launcher/restart` and `POST /v1/launcher/commands/ack`. Any other
path is a `404`; a wrong or missing token is a `401`. The token is accepted
as `Authorization: Bearer <token>` or as an `X-Sonder-Launcher-Token` header.

## Windows host setup

Run these commands in a terminal opened in the repository. Generate and store
two different values; do not paste them into source files:

```bat
py sonder_launcher.py --generate-token
setx SONDER_LAUNCHER_TOKEN "PASTE_THE_LAUNCHER_TOKEN"
setx SONDER_API_KEY "PASTE_A_DIFFERENT_MAIN_API_KEY"
setx SONDER_AUTH_MODE "api-key"
setx SONDER_LAUNCHER_CERT "C:\path\fullchain.pem"
setx SONDER_LAUNCHER_KEY "C:\path\privkey.pem"
```

`C:\pathullchain.pem` and `C:\path\privkey.pem` are a PEM certificate chain
and its private key for the name the phone connects to; the launcher serves TLS
itself and is not behind Caddy. A phone needs a publicly trusted certificate
(Caddy's `tls internal` root is not trusted by the Android app). Where to
obtain one, and the renewal restart, are in
[secure remote access](docs/runbooks/secure-remote-access.md#where-the-launchers-certificate-comes-from).

`setx` updates future command windows, not this one. Close this terminal and
open a new terminal in the repository (or set the same variables in the
current terminal), then install autostart and launch:

```bat
sonder-launcher-autostart.cmd
sonder-launcher.cmd --host 0.0.0.0 --cert C:\path\fullchain.pem --key C:\path\privkey.pem
```

The autostart installer creates a per-user Startup entry only after the token
and both TLS paths name existing files. It does not copy those values into the
entry. Remove it with:

```bat
sonder-launcher-autostart.cmd uninstall
```

To confirm the launcher is up, run this on the host in a second terminal
(replace `LAUNCHER_TOKEN`). Expect a JSON status object; a `401` means the
token is wrong:

```powershell
curl.exe -k -H "Authorization: Bearer LAUNCHER_TOKEN" https://127.0.0.1:11436/v1/launcher/status
```

`-k` is needed only because the certificate names the public name, not
`127.0.0.1`. To undo the setup, run `sonder-launcher-autostart.cmd uninstall`,
stop the launcher process, and remove each `setx` variable from your user
environment (Settings, System, About, Advanced system settings, Environment
Variables).

Open the TLS reverse-proxy port (normally `443`) and TLS launcher port `11436`
only for the trusted private network or VPN. Never open or port-forward the
loopback runtime port `11435`. Sonder Runtime does not change the
operating-system firewall automatically.

## Linux or macOS host setup

Set the same three environment variables in the account that will run the
launcher, then run:

```sh
SONDER_LAUNCHER_HOST=0.0.0.0 \
SONDER_LAUNCHER_CERT=/path/fullchain.pem \
SONDER_LAUNCHER_KEY=/path/privkey.pem \
./sonder-launcher.sh
```

Use the operating system's normal per-user service manager to start this script
at login. Keep the environment file readable only by that user. The launcher
also accepts `--cert` and `--key`, or `SONDER_LAUNCHER_CERT` and
`SONDER_LAUNCHER_KEY`, for TLS. A non-loopback launcher refuses to start
without both. An intentionally isolated development build may opt into
plaintext with `--allow-insecure-http-for-development`; never use that override
with a distributed app or bearer credentials on an untrusted network.

## App setup

In **Settings → Connection**, enter:

1. Main server URL, such as `https://sonder.example.com`.
2. Main API key.
3. Host launcher URL, such as `https://launcher.sonder.example.com`. This must be
   entered explicitly; Sonder Runtime never derives a credential-bearing control
   endpoint from the chat server URL.
4. Host launcher token.

### Reaching the PC from a phone

Connect by address when you can: `https://192.0.2.10` (a documentation
address; use your PC's), a Tailscale `100.x` address, or
`http://10.0.2.2:11435` from the Android emulator (or `127.0.0.1:11435` after
`adb reverse tcp:11435 tcp:11435`). Which names the server accepts, and the
`421 HOST_NOT_ALLOWED` fix, are in
[HTTP API & lifecycle](docs/wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed).
Proxies that keep the `Host` name are covered in the
[server-private install runbook](docs/runbooks/install-server-private.md). The
app's own rules for sending the API key over plain HTTP are only in the
[app README](app/README.md#connecting-a-phone-to-the-pc).

### Approving a refused change from the phone

In `manual` mode a file change or host program asked for over HTTP is refused,
because nobody is at the console to answer the prompt. The chat reply keeps
its text, and `sonder_receipt.refusal` names the refused call (`tool`,
`call_id`, `reason`, `remedies`). An authenticated administrator account or
the deployment API key can approve exactly that call once with
`POST /v1/approvals/<call_id>` (body `{}` or `{"ttl_seconds": 900}`); then
send the same request again (a server in local-open mode refuses that POST;
approve at the console with `/approve <call_id>`). The approval runs the
unchanged call once, expires, and leaves the mode as it was. Changing any
argument needs a new approval. `GET /v1/approvals` lists what is waiting, and
`POST /v1/approvals/revoke/<nonce>` withdraws an approval; revoking and
denying need administrator authority (or local-open). A developer account can
only list (`GET /v1/approvals`); it gets `403` on approve, deny, and revoke.
See
[HTTP API & lifecycle](docs/wiki/05-http-api-and-lifecycle.md#one-shot-approvals-over-http).

Save, open **System**, and verify that **Host Launcher** says `ready`. The
Start, Stop, and Restart controls then operate the host. A control request is
accepted immediately and shown as a persistent operation while the app polls
for progress. Closing the System page or choosing **Stop waiting** stops only
that phone's polling; it does not terminate a model download or server startup
already running on the host. Reopening System resumes the active operation.
Setup engine, Git updates, and local training stay disabled on client-only
devices because those operations require direct access to host files.

The launcher verifies the requested server transition before reporting success.
It provisions a separate loopback-only secret, sends a fresh random nonce for
each probe, and verifies the Sonder Runtime server's nonce-bound HMAC response.
The secret is never sent to the process claiming the port. An unrelated listener
on port `11435` is reported as a conflict and is never stopped or replaced.
Restart performs a complete verified Stop transition before starting the new
process, avoiding a race with the old listener.

Operations and their bounded output are stored in the per-user
`run/sonder-launcher-operations.sqlite3` ledger, so an app reconnect can
inspect the same operation. `SONDER_LAUNCHER_DB` always overrides that
location for the standalone launcher (see the table above). Only one
operation may be active. Each submitted action carries an idempotency key, so a
transport retry of that same request returns the original operation; overlapping
actions are rejected. If acknowledgement is lost, refresh status before tapping
again. First-run Start and Restart operations may run for up to 31 minutes to
allow a model bootstrap; Stop has a one-minute cap. An interrupted launcher
process never replays unfinished work automatically. Failures remain visible as
terminal operation records and are never reported as success. Requested context
sizes must resolve to 1–1,000,000 whole tokens (`8192`, `32k`, and `1m` are
examples).

### Durable command acknowledgement protocol

Clients that need recovery across a lost HTTP response may send both
`X-Sonder-Client-Id` and `X-Sonder-Command-Id` on a Start, Stop, or Restart
request. Identifiers contain 1-64 and 1-128 ASCII letters, numbers, `.`, `_`,
`:`, or `-`, respectively, and a command ID must never be reused by that
client. Legacy requests without either header continue to use the existing
`Idempotency-Key` behavior.

The launcher fsyncs a receipt before queueing the operation and fsyncs the
bounded HTTP result before returning it. Repeating the same client/command pair
and exact request replays that completed result without queueing another
operation. Reusing the pair for different content is rejected. A receipt that
has no completed result after a launcher restart is reported as `uncertain`;
the launcher never automatically retries it. Inspect the durable launcher
operation ledger and reconcile it manually.

After receiving and storing a completed response, acknowledge it with an empty
JSON object and the same two headers:

```sh
curl -X POST -H "Authorization: Bearer LAUNCHER_TOKEN" \
  -H "Content-Type: application/json" \
  -H "X-Sonder-Client-Id: phone-1" \
  -H "X-Sonder-Command-Id: restart-2026-08-08-1" \
  -d '{}' https://launcher.sonder.example.com/v1/launcher/commands/ack
```

Acknowledgement makes old result records eligible for bounded compaction. The
journal defaults beside the launcher operation database as
`sonder-launcher-command-journal.jsonl`; `SONDER_LAUNCHER_COMMAND_JOURNAL` may
override it. This protocol is an admission/replay journal, not a replacement
for polling `/v1/launcher/operations/OPERATION_ID` to observe the operation's
actual terminal state.

## Transport and mobile packaging

Distributed Android APKs require HTTPS because bearer credentials sent over
plain HTTP can be observed by other devices on the network. CI and tagged
release builds keep Android cleartext disabled. Configure the TLS reverse proxy
before entering a remote server or launcher URL in a distributed app.

Only a locally built development APK may opt into cleartext for an intentionally
trusted loopback/LAN test after generating the native project:

```sh
flutter create --org com.sonder --project-name sonder .
python ../scripts/configure_flutter_networking.py . --allow-android-cleartext-for-development
```

Omit that development-only flag for every distributed build. A build made
this way sends the API key to a plain-HTTP host only after the person allows
that exact `host:port` in the app's Settings (see the
[app README](app/README.md#connecting-a-phone-to-the-pc)); without that, the
app withholds the key and the server answers `401`. The same script
adds Apple's local-network usage explanation when an iOS or macOS native project
exists. Android's generated manifest already includes the required Internet
permission.

The launcher does not implement wake-on-LAN. The computer must be powered on
and the launcher service must already be running. For access away from home,
prefer a private VPN or authenticated HTTPS reverse proxy rather than exposing
either port directly to the public Internet.

## Diagnostics

From the host:

```sh
python sonder_launcher.py --host 127.0.0.1
```

Expect `Sonder launcher listening on http://127.0.0.1:11436`, plus the
`WARNING: ... WITHOUT authentication` line when no token is set. If startup
prints `ERROR: ...` and exits with status 2, the message names the rule from
[Launcher environment and flags](#launcher-environment-and-flags) that failed.

From another trusted machine, replace the token and host:

```sh
curl -H "Authorization: Bearer LAUNCHER_TOKEN" \
  https://launcher.sonder.example.com/v1/launcher/status
```

The status response includes `active_operation` when work is in progress. Its
`id` can be inspected without issuing another action:

```sh
curl -H "Authorization: Bearer LAUNCHER_TOKEN" \
  https://launcher.sonder.example.com/v1/launcher/operations/OPERATION_ID
```

If the launcher is reachable but server startup fails, inspect the launcher
response and the main server log under Sonder Runtime's per-user `run`
directory. The usual cause is a missing Ollama model/runtime dependency or a
LAN main-server bind without a strong `SONDER_API_KEY`.

## Behavior status

Labels follow the [documentation status vocabulary](docs/architecture/DOCUMENT-AUTHORITY-INDEX.md#documentation-status-vocabulary). Unfinished
implementation work is tracked only in the
[master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md).

| Behavior | Status | Boundary |
|---|---|---|
| Launcher status, start, stop, and restart of `sonder_headless.py` | Implemented | Four fixed operations only; no commands, paths, setup, updates, or training. |
| Durable command acknowledgement protocol | Implemented | Admission and replay journal; polling still observes the terminal state. |
| Automatic retry of an `uncertain` command receipt | Unsupported | Reconcile it manually from the durable operation ledger. |
| Wake-on-LAN | Unsupported | The host must be powered on with the launcher already running. |
| Reaching the main server by IP, emulator alias, or the PC's own name | Implemented | Any port; other names need credentials on the server or an `allowed_hosts` entry. |
| Approving one refused call from the phone | Implemented | Developer or administrator credential; one exact pending call, once, expiring. |
| Launcher `Host` allowlist | Implemented | Same policy as the main server ([rules](docs/wiki/05-http-api-and-lifecycle.md#host-names-and-421-host_not_allowed)); without a token, other names need `SONDER_LAUNCHER_ALLOWED_HOSTS`. |
| Raising the launcher start or stop time limit by environment | Unsupported | `SONDER_LAUNCHER_START_TIMEOUT` and `SONDER_LAUNCHER_STOP_TIMEOUT` are clamped to at most the defaults (1860 s and 60 s). |
| Automatic operating-system firewall changes | Unsupported | Open only the TLS proxy and launcher ports yourself. |
| Setup engine, Git updates, or local training from client-only devices | Unsupported | These operations require direct host file access. |
| Mobile stream resume and desktop-equivalent durable state | Proposed | API-007; bounded reconnect shares the typed protocol graph, but full parity is not a current contract. |
