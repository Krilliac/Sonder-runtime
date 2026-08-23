---
name: sonder-run-and-operate
description: >-
  Operate the Sonder runtime day to day: launchers, python -m sonder_runtime
  subcommands, start/stop/drain lifecycle, backups, updates, and where state,
  PIDs, and logs land on disk. TRIGGER when the user says "start the server",
  "run the REPL", "serve the API", "restart sonder", "stop sonder", "drain the
  runtime", "backup", or "deploy". DO NOT TRIGGER for creating the venv or
  installing dependencies (use sonder-build-and-env), for interpreting
  doctor/preflight/health output (use sonder-diagnostics-and-tooling), or for
  the full configuration axis reference (use sonder-config-and-flags).
---

# Run and operate the Sonder runtime

Sonder is a local-first Python 3.12 runtime wrapped around Ollama (a local LLM
server). Three listeners matter, all loopback by default:

| Port | Process | What it is |
|---|---|---|
| 11434 | `ollama` | Ollama daemon (model inference backend) |
| 11435 | `python -m sonder_runtime serve` | Sonder OpenAI-compatible HTTP API (`/v1/chat/completions`) |
| 11436 | `sonder_launcher.py` | Authenticated mobile-control supervisor (start/stop/restart over HTTP) |

Everything below is a copy-pasteable runbook. On Windows, run the `.cmd`
launchers from the repo root, or invoke the venv Python directly:
`.\venv\Scripts\python.exe -m sonder_runtime <cmd>` (POSIX:
`./venv/bin/python -m sonder_runtime <cmd>`).

When NOT to use this skill:

- Building the venv, installing requirements, packaging bundles: `sonder-build-and-env`.
- Reading a failing `doctor`/`preflight`/`status` report and deciding what it means: `sonder-diagnostics-and-tooling`.
- Every environment variable and `sonder.toml` key in depth: `sonder-config-and-flags`.

## Command anatomy: `python -m sonder_runtime`

One production entry point with subcommands (`sonder_runtime/__main__.py`).
Almost every subcommand accepts these shared flags:

| Flag | Meaning |
|---|---|
| `--config <path>` | Path to `sonder.toml` |
| `--secrets <path>` | Path to the secrets env file |
| `--set SECTION.KEY=VALUE` | Explicit config override, highest precedence, repeatable |
| `--json` | Machine-readable output |
| `--skip-ollama` | (preflight/doctor/diagnostics/smoke/serve only) do not probe the Ollama endpoint |

`python -m sonder_runtime --version` prints the build version.

### Subcommand table

| Command | Purpose | Notes |
|---|---|---|
| `serve [port]` | Run the HTTP adapter on 11435 (or the positional port) | Fail-closed preflight first; `--skip-preflight` exists for recovery work only. Refuses to bind until schema epoch 2 is adopted (see Lifecycle below). |
| `repl` | Interactive terminal REPL | What `sonder.cmd` ultimately runs. |
| `mcp [--native]` | MCP adapter (Model Context Protocol, for agent clients) | `--native` uses the application-owned bounded transport and refuses startup unless the unsafe-lab acknowledgement env (`SONDER_UNSAFE_LAB_ACK`, exact required string) is set — see `docs/runbooks/unsafe-lab.md`. |
| `preflight` | Run startup checks without binding | |
| `doctor [--skip-ollama] [--storage-probe]` | Consolidated read-only health report | `--storage-probe` runs an explicit 8 MiB / 5 second state-storage throughput probe. Interpretation: `sonder-diagnostics-and-tooling`. |
| `status` | Local build/config/schema status | |
| `diagnostics` | Redacted diagnostic bundle | |
| `config` | Show effective redacted configuration | |
| `migrate [--store memory\|autopilot\|fleet\|operations] [--adopt-epoch2]` | Apply pending schema migrations | `--adopt-epoch2` is the explicit crash-safe SPEC-5 epoch-2 bridge adoption. |
| `backup create\|list\|prune [--target DIR] [--keep N]` / `backup verify <path>` | Backup management | Default target is `<state-home>/backups`. `prune --keep N` is simple keep-N; omit `--keep` for tiered daily/weekly/monthly. |
| `restore verify\|smoke\|apply <path> [<dest>] [--confirm restore]` | Restore management | `apply` takes `<path> <destination>` and requires the literal word `restore` via `--confirm`. |
| `smoke` | Minimal end-to-end check | |
| `drain` | Graceful drain of a running server | POSTs `http://127.0.0.1:<config port>/v1/admin/drain` with `Authorization: Bearer <api key>` (when configured) and a fresh `Idempotency-Key`. |
| `update status\|build\|import\|install\|rollback\|cancel` | Signed engine updates (SPEC-4, packaged installs) | See "Updating" below. |
| `rotate-key --secrets <path> [--overlap-seconds N]` | Rotate `SONDER_API_KEY` | Previous key stays valid for the overlap window (default 86400 s = 24 h). |
| `eval-history status\|record` | Inspect or explicitly append precomputed evaluation evidence | `record` never runs a model; it appends one aggregate result to a JSONL history. |

## Windows launchers (repo root)

`sonder-runtime.cmd` is a shared prelude sourced by the others. It sets
`SONDER_HOME` (default `%LOCALAPPDATA%\sonder`), resolves `SONDER_PYTHON`
(explicit env > bundled engine runtime > `venv\Scripts\python.exe` > `python`/
`py` on PATH) and `SONDER_OLLAMA_EXE`; when a bundled `ollama.exe` exists it
also sets `OLLAMA_MODELS=%SONDER_HOME%\ollama-models` and `OLLAMA_NO_CLOUD=1`.

| Launcher | What it does |
|---|---|
| `sonder.cmd` | Full interactive path: engine bootstrap → start API server → REPL. Sets `SONDER_HOST=127.0.0.1`, `SONDER_PORT=11435`, `SONDER_CONTEXT_SIZE=8192`, `SONDER_NUM_GPU=999`, `SONDER_NUM_BATCH=512`, `OLLAMA_FLASH_ATTENTION=1`, `SONDER_EXPOSE_REASONING=1` (defaults only; your env wins). |
| `sonder-serve.cmd` | Engine bootstrap, then foreground `python -m sonder_runtime serve` (no REPL). |
| `sonder-headless-start.cmd` / `-stop.cmd` / `-status.cmd` / `-restart.cmd` | Thin wrappers over `sonder_headless.py` (below). |
| `sonder-launcher.cmd` | Mobile-control supervisor on 11436 (`SONDER_LAUNCHER_HOST`/`SONDER_LAUNCHER_PORT` override). `sonder-launcher-autostart.cmd` registers it at login. |
| `sonder-remote.cmd` | Pure remote client (`sonder_client.py`) — talks to a server, starts nothing locally. |

`sonder.cmd` toggles:

- `SONDER_TERMINAL_BOOTSTRAP=0` — skip the engine bootstrap step.
- `SONDER_TERMINAL_START_SERVER=0` — skip starting the API server.
- `SONDER_TERMINAL_VERBOSE=1` — print bootstrap output live instead of a captured log.
- `SONDER_SERVER=<url>` — run as a remote client (`sonder_client.py`) instead of the local REPL; `SONDER_TERMINAL_REMOTE=0` forces local anyway.

POSIX equivalents exist: `sonder-runtime.sh`, `sonder-serve.sh`,
`sonder-headless.sh`, `sonder-launcher.sh`.

## Headless supervisor: `sonder_headless.py`

Starts/stops/checks the Ollama daemon and the Sonder API without a console
app. Commands: `start` (default) | `engine` | `status` | `stop` | `restart`.
Flags: `--host` (default 127.0.0.1), `--port` (default 11435),
`--stop-ollama` (with `stop`: also stop Ollama), `--context-size N`,
`--allow-hosted` (sets `SONDER_ALLOW_CLOUD=1` — data leaves the machine).

```powershell
# From the repo root
.\venv\Scripts\python.exe sonder_headless.py status
.\venv\Scripts\python.exe sonder_headless.py start --host 127.0.0.1 --port 11435
.\venv\Scripts\python.exe sonder_headless.py stop --stop-ollama
.\venv\Scripts\python.exe sonder_headless.py restart
```

`engine` only ensures Ollama is up and the `sonder:latest` alias exists, then
exits 0 — that is what launchers call before starting anything. Exit codes:
0 ok, 1 start/stop did not complete, 2 startup validation refused.

PIDs and logs land in `<state-home>/run/` as `sonder_serve.pid`,
`sonder_serve.log`, `ollama.pid`, `ollama.log`.

## First run: engine bootstrap and the `sonder` model alias

`bootstrap-engine.cmd` / `bootstrap-engine.sh` → `bootstrap_engine.py`. Picks a
base model by available memory (`qwen2.5-coder:1.5b` / `:3b` / `:7b`), starts
Ollama, and always ensures the `sonder:latest` alias exists. Flags:
`--dry-run`, `--model <name>`, `--allow-cpu-offload`, `--max-vram GB`,
`--max-system-ram GB`, `--offline` (never touch pip or a model registry),
`--bundle <dir>` (explicit engine bundle).

`setup_alias.py` writes the Modelfile behind `sonder:latest`. Flags:
`--model`, `--embed-model` (default `nomic-embed-text`), `--no-embedding`,
`--ollama <exe>`, `--offline`, `--gguf <path>` (import a local GGUF, implies
offline), `--from-usb` (+ repeatable `--usb-root`).

Environment creation (venv, requirements) is `sonder-build-and-env`, not here.

## Lifecycle

### Start (source checkout, foreground)

```powershell
.\venv\Scripts\python.exe -m sonder_runtime serve        # binds 11435
```

Two fail-closed gates run before the listener opens:

1. **Preflight.** Any required failing check prints `PREFLIGHT FAIL: ...` and
   the process exits 1 without binding. `--skip-preflight` bypasses it — for
   recovery work only.
2. **Schema epoch 2.** If the state store has not adopted epoch 2, serve exits
   1 with "migration required before serve: ...; run \`migrate --adopt-epoch2\`".
   Fix: `python -m sonder_runtime migrate --adopt-epoch2`, then serve again.

Ordinary pending migrations then run before bind (MIGRATING phase); a
migration failure also refuses to bind.

### Verify it is up (API smoke)

```bash
curl http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"sonder","messages":[{"role":"user","content":"Hello"}]}'
```

```powershell
$body = @{ model = "sonder"; messages = @(@{ role = "user"; content = "Hello" }) } | ConvertTo-Json -Depth 4
Invoke-RestMethod http://127.0.0.1:11435/v1/chat/completions -Method Post -ContentType "application/json" -Body $body
```

Or `python -m sonder_runtime smoke` for the built-in end-to-end check.

### Drain without stopping

```powershell
.\venv\Scripts\python.exe -m sonder_runtime drain
```

Rejects new mutating work while letting durable steps reach a safe
checkpoint. The CLI adds the Bearer key from config and a unique
`Idempotency-Key` for you. Exit 1 means the runtime was unreachable or the
request was rejected (check auth/port).

### Stop / restart

- Headless-managed: `sonder_headless.py stop` (add `--stop-ollama` to take the
  daemon down too), `sonder_headless.py restart`.
- systemd (Linux): `sudo systemctl stop sonder` — SIGTERM triggers the same
  graceful drain; unfinished durable work is marked interrupted, never
  silently replayed. Emergency only: `sudo systemctl kill -s SIGKILL sonder`.
- Full sequence detail: `docs/runbooks/start-stop-drain.md`.

### Mobile control plane (11436)

`sonder_launcher.py` is an authenticated supervisor for phone-driven
start/stop/restart. Routes: `/v1/launcher/status`,
`/v1/launcher/operations/<id>`, `/v1/launcher/start`, `/v1/launcher/stop`,
`/v1/launcher/restart`, `/v1/launcher/commands/ack`. Operations persist across
launcher restarts in `<state-home>/run/sonder-launcher-operations.sqlite3`
(`SONDER_LAUNCHER_DB` override). It classifies the API port as `healthy`,
`stopped`, or `foreign_listener` (something else owns 11435) using an
HMAC-based health proof — interpreting those states and the proof mechanism is
`sonder-diagnostics-and-tooling` territory. See also `MOBILE_HOST_CONTROL.md`.

## Where state and output land

State home resolution order (`sonder_runtime/platform/paths.py`):

1. In-process override (`configure_home()` — programmatic only).
2. `SONDER_STATE_HOME` env, then `SONDER_HOME` env. (This is the `paths.py`
   helper's order; the typed startup config prefers `SONDER_HOME` — the
   layer-dependent alias trap is documented in `sonder-config-and-flags`.
   Set exactly one of the two.)
3. Per-OS default:
   - Windows: `%LOCALAPPDATA%\sonder` (falls back to `%APPDATA%`, then `%USERPROFILE%\AppData\Local\sonder`).
   - macOS: `~/Library/Application Support/sonder`; a pre-existing legacy `~/.local/share/sonder` keeps winning until a native store exists.
   - Linux: `$XDG_DATA_HOME/sonder`, else `~/.local/share/sonder`.

Layout inside the state home (each DB has a matching env override, e.g.
`SONDER_JOBS_DB` — full axis in `sonder-config-and-flags`):

| Path | Contents |
|---|---|
| `memory.db` | Conversational/semantic memory (`SONDER_DB` override; a legacy repo-root `memory.db` is auto-migrated here once, under a crash-safe lock) |
| `autopilot.db`, `fleet.db`, `operations.db`, `queued_actions.db` | Durable task/agent state |
| `updates.db`, `jobs.db`, `sessions.db`, `extensions.db`, `goals.db`, `fanout.db` | Update ledger, job queue, sessions, extensions, goals, fan-out store |
| `embed-cache.db` | Embedding cache |
| `runtime_policy.json`, `training_state.json`, `branch_predictor.json` | Policy and adaptive state |
| `workflows.json` | Operator-owned saved workflows |
| `npu-manifests/` | NPU accelerator manifests |
| `dumps/` | Debug dumps |
| `run/` | PID files, `sonder_serve.log`, `ollama.log`, launcher operations DB |
| `selfmod/` | Self-modification DB, backups, and working areas (`SONDER_SELFMOD_HOME`) |
| `backups/` | Default `backup create` target |
| `ollama-models/` | `OLLAMA_MODELS` when using a bundled Ollama (Windows launchers) |

## Backups and restore (operational quick path)

```powershell
python -m sonder_runtime backup create                 # to <state-home>/backups
python -m sonder_runtime backup list
python -m sonder_runtime backup verify <backup-path>
python -m sonder_runtime backup prune                  # tiered daily/weekly/monthly
python -m sonder_runtime backup prune --keep 5         # simple keep-N

python -m sonder_runtime restore verify <backup-path>
python -m sonder_runtime restore smoke  <backup-path>
python -m sonder_runtime restore apply  <backup-path> <destination> --confirm restore
```

Always `verify` (and preferably `smoke`) before `apply`. Full incident flow:
`docs/runbooks/backup-restore.md`.

## Updating

Two distinct mechanisms — do not mix them up:

### Packaged installs: signed bundles (`update` subcommand)

```powershell
python -m sonder_runtime update status
python -m sonder_runtime update import <bundle-path>          # prints a confirm nonce when available
python -m sonder_runtime update install <update-id> --confirm <nonce>
python -m sonder_runtime update rollback --confirm <last-8-of-previous-release-id>
```

Bundles are signature-verified (TUF metadata). `--allow-unverified` on
import/install additionally requires `SONDER_UPDATE_ALLOW_UNSIGNED=1` in the
environment and is never for production. `install` takes a pre-install backup
unless `--skip-backup` (testing only). Runbook:
`docs/runbooks/upgrade-rollback.md`.

### Source checkouts: guarded fast-forward

- In the REPL: `/updatecheck` (explicit network refresh of update state), then
  `/update`. The guarded update refuses anything that is not the canonical
  origin remote on branch `main` with a clean tree and zero local commits; it
  runs `git merge --ff-only origin/main` with hooks disabled. Restart the
  process after a successful update.
- `/stash save` | `/stash save-untracked` | `/stash pop` manages a single
  fixed-message recovery stash so a dirty checkout can be parked, updated,
  and restored. `pop` requires a clean tree and an existing recovery stash.
- Outside the REPL: `python safe_update.py --repo <checkout>` does
  `git stash push --include-untracked` → `fetch origin main` →
  `rebase origin/main` → `stash apply`. Exit 2 after a stash-apply conflict
  means your edits are kept in the stash and need manual resolution
  (`git stash list`).

## Linux service deployment

```bash
git clone https://github.com/Krilliac/Sonder-runtime.git && cd Sonder-runtime
sudo bash deploy_sonder.sh --serve     # --serve-only skips the model step
```

Run as root from inside the checkout. It installs Ollama + a venv, writes
`/etc/sonder/sonder-local.env` (mode 0600, auto-generated random
`SONDER_API_KEY` unless you export one), and installs systemd unit
`sonder.service` with `ExecStart=<venv-python> -m sonder_runtime serve <port>`
(port from `SONDER_PORT`, default 11435), `Environment=SONDER_HOST=127.0.0.1`,
`Restart=on-failure`, `RestartSec=3`, `UMask=0077`, `NoNewPrivileges=true`.

Manage it:

```bash
systemctl status sonder
journalctl -u sonder -f
sudo systemctl restart sonder
```

This path is **loopback-only by design — never open or port-forward 11435**.
For remote clients use the server-private profile plus the TLS reverse proxy:
`docs/runbooks/install-server-private.md` and
`docs/runbooks/secure-remote-access.md` (point operators there; do not
improvise exposure).

## Multi-PC inference workers (operational summary)

`SONDER_OLLAMA_WORKERS` lists additional Ollama worker origins; any non-local
origin must be `https` with an explicit port and requires
`SONDER_ALLOW_REMOTE_OLLAMA=1`. In one line: the pool schedules
least-inflight and fails over only when no response was received. Scheduling
and failover semantics with the exact constants:
`sonder-agents-and-fleets`. Setup and certificates:
`docs/runbooks/multi-pc-ollama.md`; config-value details:
`sonder-config-and-flags`.

## Runbook index — open when the situation matches

All under `docs/runbooks/`:

| Situation | Runbook |
|---|---|
| Starting, stopping, draining, SIGKILL aftermath | `start-stop-drain.md` |
| Taking or restoring a backup, verifying integrity | `backup-restore.md` |
| Upgrading a deployment or rolling back a bad release | `upgrade-rollback.md` |
| Ollama down/unreachable, inference errors | `ollama-outage.md` |
| Disk filling up under the state home | `disk-exhaustion.md` |
| SQLite "database is locked" or suspected corruption | `database-lock-or-corruption.md` |
| Adding remote Ollama workers | `multi-pc-ollama.md` |
| Rotating the API key or other credentials | `rotate-credentials.md` |
| A secret may have leaked | `suspected-secret-exposure.md` |
| Cutting a release / version policy | `publish-release.md`, `release-version-policy.md` |
| Fresh installs (workstation / private server) | `install-workstation-local.md`, `install-server-private.md` |
| Enabling the unsafe lab (native MCP etc.) | `unsafe-lab.md` |
| Autopilot interrupted mid-task | `autopilot-interruption.md` |
| Training run failed | `training-failure.md` |
| Remote/TLS access, model collections, facts model, branch cleanup | `secure-remote-access.md`, `assemble-model-collection.md`, `use-facts-model.md`, `merged-branch-cleanup.md` |

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Re-verify with:

- Subcommands and flags: `python -m sonder_runtime --help` and read `build_parser()` in `sonder_runtime/__main__.py`.
- Ports: `rg -n "11435|11434|11436" sonder_runtime/platform/config.py sonder_launcher.py sonder.cmd`
- State home rules: read `default_home()` in `sonder_runtime/platform/paths.py`.
- State file names: `rg -o 'state_path\("[^"]+"' --no-filename | sort -u`
- Headless CLI: read `sonder_runtime/interfaces/cli/headless.py` (choices and flags near the top of `run()`).
- Launcher env defaults: read `sonder.cmd` and `sonder-runtime.cmd`.
- Serve gates (preflight, epoch 2): read `cmd_serve()` in `sonder_runtime/__main__.py`.
- Guarded source update rules: read `runtime_update()` and `runtime_stash()` in `git_tools.py`.
- systemd unit contents: `rg -n "ExecStart|SONDER_HOST|NoNewPrivileges" deploy_sonder.sh`
- Worker pool semantics: read `sonder_runtime/adapters/inference/ollama_pool.py` (cooldown/threshold constants at top).
- Runbook list: `ls docs/runbooks/`
