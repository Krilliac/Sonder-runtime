# Mobile host control for Sonder Runtime

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
orchestrated. The main Sonder API remains on port `11435`. Use a different
strong credential for each service:

- `SONDER_API_KEY` authenticates chat and System API requests.
- `SONDER_LAUNCHER_TOKEN` authenticates host process control.

Both must contain at least 24 characters for a LAN bind.

## Windows host setup

Run these commands in a terminal opened in the repository. Generate and store
two different values; do not paste them into source files:

```bat
py sonder_launcher.py --generate-token
setx SONDER_LAUNCHER_TOKEN "PASTE_THE_LAUNCHER_TOKEN"
setx SONDER_API_KEY "PASTE_A_DIFFERENT_MAIN_API_KEY"
setx SONDER_AUTH_MODE "api-key"
sonder-launcher-autostart.cmd
```

Sign out and back in so the user environment is refreshed, or set the same
variables in the current terminal for the first run. Then launch:

```bat
sonder-launcher.cmd --host 0.0.0.0
```

The autostart installer creates a per-user Startup entry. It does not copy the
token into that entry. Remove it with:

```bat
sonder-launcher-autostart.cmd uninstall
```

Open TCP ports `11435` and `11436` in the host firewall only for the trusted
private network or VPN. Sonder Runtime does not change the operating-system
firewall automatically.

## Linux or macOS host setup

Set the same three environment variables in the account that will run the
launcher, then run:

```sh
SONDER_LAUNCHER_HOST=0.0.0.0 ./sonder-launcher.sh
```

Use the operating system's normal per-user service manager to start this script
at login. Keep the environment file readable only by that user. The launcher
also accepts `--cert` and `--key`, or `SONDER_LAUNCHER_CERT` and
`SONDER_LAUNCHER_KEY`, for TLS.

## App setup

In **Settings → Connection**, enter:

1. Main server URL, such as `http://192.168.1.20:11435`.
2. Main API key.
3. Host launcher URL, such as `http://192.168.1.20:11436`. This must be
   entered explicitly; Sonder Runtime never derives a credential-bearing control
   endpoint from the chat server URL.
4. Host launcher token.

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
`run/launcher-operations.sqlite3` ledger, so an app reconnect can inspect the
same operation. `SONDER_LAUNCHER_DB` can override that location. Only one
operation may be active. Each submitted action carries an idempotency key, so a
transport retry of that same request returns the original operation; overlapping
actions are rejected. If acknowledgement is lost, refresh status before tapping
again. First-run Start and Restart operations may run for up to 31 minutes to
allow a model bootstrap; Stop has a one-minute cap. An interrupted launcher
process never replays unfinished work automatically. Failures remain visible as
terminal operation records and are never reported as success. Requested context
sizes must resolve to 1–1,000,000 whole tokens (`8192`, `32k`, and `1m` are
examples).

## Transport and mobile packaging

HTTPS is recommended because bearer credentials sent over plain HTTP can be
observed by other devices on the network. For an intentionally trusted LAN or
VPN, CI explicitly enables Android cleartext access so existing `http://` host
URLs work. Local builds make that choice after generating the native project:

```sh
flutter create --org com.sonder --project-name sonder .
python ../scripts/configure_flutter_networking.py . --allow-android-cleartext
```

Omit `--allow-android-cleartext` for an HTTPS-only Android build. The same
script adds Apple's local-network usage explanation when an iOS or macOS native
project exists. Android's generated manifest already includes the required
Internet permission.

The launcher does not implement wake-on-LAN. The computer must be powered on
and the launcher service must already be running. For access away from home,
prefer a private VPN or authenticated HTTPS reverse proxy rather than exposing
either port directly to the public Internet.

## Diagnostics

From the host:

```sh
python sonder_launcher.py --host 127.0.0.1
```

From another trusted machine, replace the token and host:

```sh
curl -H "Authorization: Bearer LAUNCHER_TOKEN" \
  http://HOST:11436/v1/launcher/status
```

The status response includes `active_operation` when work is in progress. Its
`id` can be inspected without issuing another action:

```sh
curl -H "Authorization: Bearer LAUNCHER_TOKEN" \
  http://HOST:11436/v1/launcher/operations/OPERATION_ID
```

If the launcher is reachable but server startup fails, inspect the launcher
response and the main server log under Sonder Runtime's per-user `run`
directory. The usual cause is a missing Ollama model/runtime dependency or a
LAN main-server bind without a strong `SONDER_API_KEY`.
