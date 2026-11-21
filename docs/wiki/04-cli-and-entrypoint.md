# CLI & Entry Point

`python -m sonder_runtime <command>` is the single supported entry point
for production operations. The historical launch scripts remain as
compatibility surfaces and delegate here.

## Commands

| Command | Purpose |
|---|---|
| `serve [port]` | Run the HTTP adapter. Runs preflight, the epoch-2 gate and migrations before binding; refuses to bind if any of them fails (see [Startup order](#startup-order)). A positional `port` overrides `[server].port`. `--skip-preflight` skips only the preflight (recovery work). |
| `mcp` | Run the MCP adapter (tool surface for MCP clients). `--native` selects the application-owned bounded transport; `--progressive-tools` (needs `--native`, else exit 2) exposes tool search and loads selected schemas on demand. |
| `repl` | Interactive REPL with slash commands. `--json` emits `sonder.repl-output.v1` JSON Lines on stdout with no terminal chrome. |
| `preflight` | Run startup checks and report; opens no listener. |
| `doctor` | Consolidated health report for config, state/model storage, schema migrations, schema-epoch adoption, backup freshness, self-heal, memory quality, runtime policy, and Ollama reachability. `schema_epoch` is FAIL (exit 1) for any home whose SPEC-5 domain databases are not all stamped at epoch 2 (`sonder_doctor.py:240-296`). On a home that already holds data this means `serve` will refuse to start until `migrate --adopt-epoch2` has run. On a brand-new, untouched home it is a false alarm: `serve`, `migrate`, `repl` and `mcp` stamp a truly fresh home themselves (`stamp_fresh_home`, `__main__.py:1180-1206,1283`) and the doctor check does not model that, so start `serve` (or `migrate`) instead of running the repair (see [install-workstation-local](../runbooks/install-workstation-local.md#fresh-state-home-and-the-epoch-2-gate)). Self-heal and memory quality inspect `SONDER_DB` when set, otherwise `<state home>/memory.db` of the selected configuration; a missing or not-yet-initialized database is reported as skipped. Doctor never creates, initializes or migrates a database (the memory database is opened `mode=ro`, which may leave SQLite `-wal`/`-shm` sidecar files); storage inspection writes only when the explicit probe flag is supplied. |
| `status` | Local build / config / schema status. |
| `diagnostics` | Redacted diagnostic bundle (config, schemas, preflight). |
| `config` | Print the effective, redacted configuration. |
| `migrate` | Apply pending schema migrations (all stores or `--store`). `--adopt-epoch2` runs the crash-safe SPEC-5 epoch-2 adoption that `serve` requires; it backs the databases up to `backups/pre-epoch2-*` first, is a verified no-op (no new copy) when the home is already adopted, and rejects `--store` (exit 2) because adoption always covers every domain database. |
| `backup` | `create` / `verify` / `list` / `latest` / `prune`. |
| `restore` | `verify` / `smoke` / `apply` a backup, or `rehearse` the offline recovery rehearsal against one in a disposable workspace. |
| `smoke` | Minimal end-to-end check (config, migrate, ops roundtrip). |
| `drain` | Request graceful drain of a running server (`POST /v1/admin/drain` on `127.0.0.1:<[server].port>`, with the configured API key as bearer). The server exits after draining. Exit 1 if it cannot be reached or rejects the request. |
| `rotate-key` | Rotate `SONDER_API_KEY` with an overlap window. The secrets file is `--secrets`, else `SONDER_SECRETS`, else `sonder.env` in the state home. `--config`/`--set` choose the state home that receives `secrets/rotation.json` and the `API_KEY_ROTATED` audit event; the configuration is validated as `serve` validates it, and an invalid one exits 2 before anything is rotated. |
| `update` | `status` / `build` / `import` / `install` / `rollback` / `cancel` (see [Update Manager](13-update-manager.md)). Takes no `--config`, `--secrets` or `--set`. |
| `capabilities refresh` | Probe the configured local Ollama models and record measured capability evidence. `--model` (repeatable subset), `--timeout` (seconds per model, default 120, maximum 300), `--context-tokens`. Exit 0 only when every probed model is `measured`, else 1. |
| `models cloud-refresh` | Probe configured Ollama cloud models; retire unavailable ones, record the catalog, and rebind from the operator ladder. A dry run unless `--apply`; `--no-catalog` skips reading the public `ollama.com` catalog. Accepts `--config`, `--secrets`, `--set`, `--json`. |
| `playbooks` | Owner-curated playbook notes: `list`, `show`, `approve`, `reject`, `rm`, `edit`, `merge [--apply]` (see [Playbooks](22-playbooks.md)). Accepts `--config`, `--secrets`, `--set`; `--json` is per subcommand. These are options of the parent parser, so they go before the subcommand: `playbooks --config <toml> list` (`interfaces/cli/playbooks.py:170-173`). |
| `eval-history` | `status` reads identity-separated trends (optional filters `--model`, `--model-digest`, `--suite`, `--suite-version`, `--suite-digest`, `--tolerance`); `record` appends one precomputed aggregate result and never runs a model (requires `--model`, `--model-digest`, `--suite`, `--suite-version`, `--suite-digest`, `--passed`, `--total`; optional `--recorded-at`, `--source`). Both take `--history <jsonl>` and `--json`; neither takes `--config`. |
| `artifact-mobility` | Explicit local fixed-peer artifact copy: `send --source-artifact <id> --confirm-destination <peer>`, `resume --operation-id <id>`, `status --operation-id <id>`, `list`. An invalid request exits 2 with `INVALID_REQUEST`. Operator-only; never a model-facing tool. |
| `control-state-rehearsal` | Collect disposable external control-state evidence without promotion. Requires `--config` (a rehearsal-only configuration), `--event-id`, `--resource-kind {job}`, `--resource-id`, `--owner-epoch`, `--sequence` and `--payload-digest` (optional `--secrets`, `--json`, `--confirm-fence`, `--new-owner-id`). Exit 2 on rejected arguments. |
| `openrouter` | `models [--search S] [--tools]`, `account` (both read-only) and `use <tier> <vendor/model or none> [--verify]` (maps a tier to an OpenRouter model in the runtime policy; `none` clears it). Tiers: `fast`, `general`, `code`, `reasoning`, `vision`. Takes only `--json`; exit 2 on any error. |
| `anthropic` | `status [--json]`: read-only provider status (configuration, key slots as booleans, SDK installed, default model). Also reports today's local spend against `SONDER_ANTHROPIC_DAILY_USD` (`bootstrap/anthropic_cli.py:1-8`). Local only: no network call. |

Common flags: `--config <toml>`, `--secrets <env>`, `--set section.key=value`
(highest precedence; repeatable), `--json`, and `--skip-ollama` where a check
would otherwise probe the model server. How `--set` ranks against the TOML,
the secrets file and the environment has one home:
[Configuration: Precedence](03-configuration.md#precedence-production-config).

Per-command flags (checked against `sonder_runtime/__main__.py:1894-2178`):

| Command | Flags beyond `--config`/`--secrets`/`--set`/`--json` |
|---|---|
| `preflight`, `diagnostics`, `smoke`, `serve` | `--skip-ollama` (no Ollama probe) |
| `doctor` | `--skip-ollama`, `--skip-inference` (no Sonder Inference probe), `--storage-probe`, `--alert` (notify on failed checks and missed scheduled jobs per `[health_alerts]`; writes the rate-limit state file) |
| `serve` | `[port]`, `--skip-preflight` |
| `repl` | none (no `--skip-ollama`) |
| `mcp` | `--native`, `--progressive-tools` |
| `migrate` | `--store {memory,autopilot,fleet,operations,queued_actions,updates,jobs}` or `--adopt-epoch2`; the two together exit 2 |
| `backup create`, `list`, `latest`, `prune` | `--target <dir>`; `prune` also `--keep N` (omit for tiered daily/weekly/monthly). `latest` prints the newest valid backup path and exits 1 when there is none |
| `rotate-key` | `--overlap-seconds N` (default 86400; accepted range 60 to 604800) |
| `models cloud-refresh` | `--apply` (write the overlay, state file and change log; default is a dry run), `--no-catalog` |

Commands with **no** `--config`/`--secrets`/`--set`: `backup verify <path>`,
every `restore` subcommand, every `update` subcommand, `eval-history`,
`artifact-mobility`, `openrouter` and `anthropic`. `control-state-rehearsal`
takes `--config` (required) and `--secrets` but no `--set`.

## Typical sessions

```bash
# Bring a fresh install up (a state home with no databases yet)
python -m sonder_runtime preflight --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env
python -m sonder_runtime serve     --config /etc/sonder/sonder.toml --secrets /etc/sonder/sonder.env

# Operate
python -m sonder_runtime status --json
python -m sonder_runtime doctor --json
python -m sonder_runtime doctor --skip-ollama  # fully local checks only
python -m sonder_runtime doctor --storage-probe # explicit bounded state-volume benchmark
python -m sonder_runtime backup create --json
python -m sonder_runtime restore smoke /var/backups/sonder/<dir>
python -m sonder_runtime rotate-key --secrets /etc/sonder/sonder.env --overlap-seconds 86400
python -m sonder_runtime drain     # asks the running server to drain; the server exits after draining
```

On a **fresh** state home `serve`, `migrate`, `repl` and `mcp` stamp epoch 2
automatically before creating stores; do not run `smoke` first, because it
migrates `operations.db` without the stamp and the next `serve`, `repl` or
`mcp --native` then exits 1. Mechanism, symptom and recovery have one home:
[Install on a workstation: fresh state home and the epoch-2 gate](../runbooks/install-workstation-local.md#fresh-state-home-and-the-epoch-2-gate).
Run `migrate` yourself for an **existing** home (upgrades) and
`migrate --adopt-epoch2` for a pre-epoch-2 home.

The automatic storage checks report free space for the configured state home
and the configured or platform-native Ollama model root. They use native volume
metadata where safely available and warn on network, removable, or potentially
slow filesystems. Paths derive from configuration, environment, and the current
user profile; no drive letter or machine-specific layout is assumed.

`--storage-probe` is never implied by `doctor`, `status`, preflight, or service
startup. When explicitly selected it probes only the existing configured state
directory. The probe is capped at 8 MiB and 5 seconds and uses one worker-owned
anonymous handle on supporting systems or delete-on-close handle on Windows. It
never exposes or reopens a generated pathname. A scrubbed, isolated worker owns
the handle; a fixed-size result pipe is the only output, and the parent kills
the worker at the five-second wall deadline so process teardown cleans up
blocked setup, I/O, sync, or close. The probe does not scan files, alter model
data, or probe every mounted volume.

## Exit codes

- `0` success; `1` operational failure (e.g. preflight/migration/backup
  failed, `doctor` reported a FAIL, `drain` could not reach the server);
  `2` configuration/usage error (fails before any side effect);
  `130` interrupted (`__main__.py:2181-2224`).
- `doctor` exits `1` only when the overall result is FAIL; warnings exit `0`
  (`__main__.py:456`). `preflight` exits `0` or `1` (`:401`).
- Exception by design: `status` and `diagnostics` are always-available
  reports for collecting evidence from a broken install. On an invalid or
  missing `--config`/`--secrets` they still exit `0` and emit their payload,
  with the problems under `config_errors` and a `WARNING: configuration is
  invalid` line on stderr. Gate scripts on `config`, `doctor` or `preflight`,
  which exit `2` for the same input.
- Launcher scripts add their own codes (`3` for no Python in most of them,
  `1` in `endless-train.cmd`; `2` when the local engine cannot start); see
  [Launchers and setup scripts](#launchers-and-setup-scripts).

## Startup order

- **`serve`**: configuration load (exit 2 on error) -> **preflight** (unless
  `--skip-preflight`; exit 1 on a failed required check) -> **epoch-2 gate**
  (`stamp_fresh_home`, then `require_epoch_2`; exit 1 with
  `migration required before serve`) -> **migrations** (`migrate_all`; exit 1
  on failure) -> audit-event prune (`[observability].audit_retention_days`,
  default 90) -> compose the application -> **bind**
  (`__main__.py:1209-1345`). A failed required check, gate or migration means
  no socket ever opens: the fail-closed contract from
  [Configuration](03-configuration.md).
- **`repl`** runs no preflight. It stamps a fresh state home and enforces the
  epoch-2 gate (`_stamp_home_before_stores(gate=True)`, exit 1 with `migration
  required before startup` on an unadopted home), then runs `migrate_all` (exit
  1 on failure) (`__main__.py:1431-1440`). `mcp --native` does the same
  (`:1501-1509`); see the
  [epoch-2 runbook](../runbooks/install-workstation-local.md#fresh-state-home-and-the-epoch-2-gate). Non-native `mcp` stamps a fresh state home
  (`_stamp_home_before_stores(gate=False)`, `:1539`) and does not call
  `migrate_all` in `__main__.py`; it then configures and composes the
  application. Both `mcp` modes refuse to
  start (exit 2) when an unsafe-lab activation is invalid.
- **`serve`, `repl` and `mcp`** all start the static inference membership
  controller after composing the application
  (`bootstrap/app.py:2330-2348`, called from `__main__.py:1330`, `:1460`,
  `:1513`, `:1570`). It does nothing unless remote Ollama workers are
  configured and `[membership].mode` is not `external`.

## REPL slash commands (selection)

`/stats` (learning/token stats), `/activity` (current response actions),
`/permissions` and `/filepolicy` (guardrails), `/run [sec]` (execute the
last code block, guarded), `/train [N]` (grounded practice),
`/autopilot status|resume|cancel`, `/runtime status`, `/pass` `/fail`
(record outcomes), `/sessions` `/replay [id|title] [N]` `/resume <id|title>`
(list, re-render read-only, and continue past threads). Account commands:
`/register <user> <password>` (the first account becomes admin),
`/login [<user> <password>]` (prompts with a masked password when given no
arguments; the token is kept for the session and never printed), `/whoami`,
`/admin`, `/accounts` (admin) and
`/setaccount <user> role= tier= dev_flags= banned=` (admin)
(`interfaces/repl/repl.py:1835-1840`, `:4661-4704`). Setup and the account
model: [Configure authentication](../runbooks/configure-authentication.md).
Plain English also
triggers many of these. Dangerous commands over HTTP require
developer/admin authorization.

The REPL has no memory-replication send, retry, promotion, takeover, or
failback command. `/runtime status` is read-only and does not contact a
replication peer. The bounded fact-only service's `replicate_once()` method is
an application-owned operator seam with no public CLI, MCP, REPL, or HTTP
mutation surface. Use the authenticated status/API and System screen only to
inspect its truthful capability posture; see
[Bounded, explicit fact replication](../runbooks/memory-replication.md).

## Scripted REPL output

Piped REPL use (`sonder < script.txt`, `echo /stats | sonder`) prints plain
text with no terminal chrome; that shape is a stable scripting contract.
Setting `SONDER_REPL_NDJSON=1` opts a piped session into JSON on every stdout
line: each completed chat turn is one `sonder.repl-turn.v1` object (`answer`,
`error`, `elapsed_ms`, `interaction_id`, `feedback_offered`, `label`, and
`hint` — the known-failure next step, `""` when none applies), and every other
line (slash-command output, notices) becomes a `sonder.repl-output.v1`
`output` event (`schema`, `seq`, `event`, `text`).
The flag never changes interactive terminals, and the flagless piped
default never changes. Known failure shapes additionally get a one-line
`hint:` under the interactive error panel only — piped output stays
byte-stable.

## REPL logs

`python -m sonder_runtime repl` writes its log records to
`SONDER_HOME/logs/repl.log` (JSON, owner-only `0600`, rotating 5 x 1 MB) at
`SONDER_REPL_LOG_LEVEL` (default `INFO`), so no JSON line lands on the
terminal. On a terminal, WARNING and above are also queued and shown between
turns as one short notice; piped and `--json` runs print only ERROR records
to stderr, as text. `SONDER_REPL_LOG_STDERR=1` restores the old behaviour:
JSON on stderr at `SONDER_REPL_LOG_LEVEL` (default `WARNING`). `serve` and
`mcp` logging is unchanged. `/logs [n]` shows the last `n` records (default
20), with control characters escaped.

## REPL terminal behaviour

- **Terminal detection:** colour, glyphs and motion are decided once at start
  (`sonder_runtime/interfaces/repl/style.py`). `NO_COLOR` removes colour,
  `TERM=dumb` or `SONDER_PLAIN=1` also switch to ASCII glyphs and no
  redrawing, and `SONDER_GLYPHS` / `SONDER_THEME` override the guesses.
  The REPL cannot see a terminal's background, so it draws with the
  terminal's own 16 colours until the theme is known:
  `SONDER_THEME=dark` (or `light`) on a 256-colour or truecolor terminal
  (Windows Terminal counts as truecolor) switches to the app's palette.
- **Piped output:** when stdout is not a terminal there is no prompt and no
  status line, and the banner goes to stderr as one line. With
  `SONDER_REPL_NDJSON=1`, every stdout line is JSON. Model answers, file
  contents and errors are shown with control characters escaped, on a
  terminal and on a pipe alike.
- **Approval prompts:** keys typed before the prompt appears are discarded.
  The prompt accepts `y`, `yes`, `n`, `no` or Enter (no). A `[danger]`
  command needs the whole word `yes`. A slash command typed as the answer
  counts as no, and it is saved to history so Up recalls it.
- **Line editing:** on Linux and macOS terminals, readline provides history,
  editing and Tab completion. Leftover arrow-key escape sequences are
  removed before a line is used.
- **History:** a terminal session saves its history to
  `SONDER_HOME/repl_history` (`0600`, newest 200 lines, credential lines
  excluded). `SONDER_REPL_HISTORY=0` keeps history in memory only.
- **Commands:** `/about` shows the version, working directory, source
  revisions, endpoint, session and mode, and what was detected about the
  terminal. `/status` shows the full status line plus the Ollama pool in plain
  words, and `/status pool` shows the pool detail. `/help status` explains
  each status-line field.
- **Ctrl-C:** at the prompt, the first press clears the line and a second
  press within 2 s quits. During a turn, Ctrl-C cancels only that turn.

## Launchers and setup scripts

The scripts in the repository root are thin wrappers around
`python -m sonder_runtime` and two helper programs. This section is their one
reference home (checked 2026-11-20). Use the `.cmd` form on Windows and the
`.sh` form on Linux/macOS. The Ollama tuning defaults listed under
[`sonder.cmd` behaviour](#sondercmd-behaviour) (`SONDER_NUM_THREAD`,
`SONDER_NUM_GPU`, `SONDER_NUM_BATCH`, `OLLAMA_FLASH_ATTENTION`) are set only
by `sonder.cmd`, `sonder-serve.cmd`, `sonder-headless.cmd`,
`bootstrap-engine.cmd`, `endless-train.cmd` and `endless-train.sh`. The other
launchers (including `sonder-launcher.*`, `sonder-remote.cmd` and the other
`.sh` wrappers) rely on the caller's environment, except that
`bootstrap_engine.py` applies the same four defaults to its `setup_alias.py`
child on every platform (`bootstrap_engine.py:444-447`).

| Script | What it does |
|---|---|
| `sonder.cmd` | Windows terminal launcher: selects Python, brings up the engine and a background API server, then starts the REPL. Details below. |
| `sonder-runtime.cmd` / `sonder-runtime.sh` | Not run directly. `call`ed (`.cmd`) or sourced (`.sh`) by the launchers that use the bundled runtime. `sonder-remote.cmd` (own order: `SONDER_PYTHON`, then `venv\Scripts\python.exe`, then bare `python`, with no missing-Python check), `sonder-safe-update.cmd` and `sonder-launcher-autostart.cmd` do not use it. |
| `sonder-serve.cmd` / `sonder-serve.sh` | Run `sonder_headless.py engine` (Ollama reachable and the `sonder:latest` alias present), then `python -m sonder_runtime serve "$@"` in the foreground. Exit 3 with no Python; `.cmd` exits 2 when the engine step fails. |
| `sonder-headless.cmd` / `sonder-headless.sh` | Background supervisor for Ollama and the API server: `start` (default), `engine`, `status`, `stop`, `restart`. `sonder-headless-start.cmd`, `-stop.cmd`, `-status.cmd` and `-restart.cmd` are one-word wrappers. |
| `sonder-launcher.cmd` / `sonder-launcher.sh` | Authenticated start/stop supervisor for mobile apps; see [Mobile host control](../../MOBILE_HOST_CONTROL.md) for the token and TLS setup. `sonder-launcher-autostart.cmd` installs a Windows Startup entry and refuses (exit 2) without `SONDER_LAUNCHER_TOKEN`, `SONDER_LAUNCHER_CERT` and `SONDER_LAUNCHER_KEY` (the cert and key must name existing files). The entry runs `sonder-launcher.cmd --host 0.0.0.0`, so the launcher listens on all interfaces at logon (`sonder-launcher-autostart.cmd:31`). `sonder-launcher-autostart.cmd uninstall` removes the entry. |
| `sonder-remote.cmd` | Runs `sonder_client.py`, the thin client for a hosted server; see [Client](../../CLIENT.md). |
| `sonder-safe-update.cmd` | Updates a **git checkout**: refuses outside a git work tree, saves local edits to a uniquely tagged stash, `git fetch origin main`, `git rebase origin/main`, then re-applies the edits. On a failed rebase it aborts and restores the previous revision. Exit 0 updated, 1 failed or restored, 2 updated but the saved edits conflict (stash kept). For signed bundle updates use `update`, see [Update Manager](13-update-manager.md). |
| `bootstrap-engine.cmd` / `bootstrap-engine.sh` | Wrapper for `bootstrap_engine.py` (flags below). Exit 3 with no Python. |
| `endless-train.cmd` / `endless-train.sh` | Endless grounded-practice loop (`endless_train.py`) until Ctrl+C or a round with no useful progress; tuned by `SONDER_ENDLESS_*` variables. See [Training](15-training.md). |

### How the runtime is chosen (`sonder-runtime.*`)

Python, in order: `SONDER_PYTHON`; the engine bundle's bundled Python; the
checkout's `venv` (`venv\Scripts\python.exe`, or `venv/bin/python3`); then
`python.exe`/`py.exe` (Windows) or `python3`/`python` (POSIX) on `PATH`
(`sonder-runtime.cmd:21-28`, `sonder-runtime.sh:43-63`). The engine bundle is
`SONDER_ENGINE_BUNDLE` if set, else `engine/<platform>/ENGINE-BUNDLE.json` or
`engine/ENGINE-BUNDLE.json` in the checkout. With a bundle that ships Ollama,
`SONDER_OLLAMA_EXE` points at it, `OLLAMA_MODELS` defaults to
`<sonder-home>/ollama-models` and `OLLAMA_NO_CLOUD=1`; otherwise
`SONDER_OLLAMA_EXE` defaults to the literal `ollama` on Windows
(`sonder-runtime.cmd:36`) and, on POSIX, to the path `command -v ollama`
resolves, or empty when none is found (`sonder-runtime.sh:71`). `SONDER_HOME`
defaults to `%LOCALAPPDATA%\sonder` (else `%USERPROFILE%\.sonder`) on
Windows. On POSIX, `$XDG_DATA_HOME/sonder` wins on every platform, including
macOS (`sonder-runtime.sh:6-7`); otherwise it is
`~/Library/Application Support/sonder` on macOS (an existing legacy
`~/.local/share/sonder` is kept) and `~/.local/share/sonder` on Linux. If no
Python is found, `sonder.cmd`, `sonder-serve.*`, `sonder-headless.*`,
`bootstrap-engine.*` and `endless-train.sh` print `no bundled or system Python
runtime was found` and exit 3; `endless-train.cmd` prints the same message and
exits 1 (`endless-train.cmd:21-24`); `sonder-launcher.*` print `no Python
runtime found` and exit 3.

### `sonder.cmd` behaviour

1. `sonder --version` / `-V` prints the version and exits before any engine work.
2. Unless `SONDER_TERMINAL_BOOTSTRAP=0`, runs `sonder_headless.py engine`
   (output hidden unless it fails; exit 2 and the log is printed when the
   engine is unavailable or blocked by endpoint policy).
3. Unless `SONDER_TERMINAL_START_SERVER=0`, starts the API server in the
   background on `SONDER_HOST` (default `127.0.0.1`) and `SONDER_PORT`
   (default `11435`). A failed start prints a warning and the REPL still opens.
4. If `SONDER_SERVER` is set, runs the thin client `sonder_client.py` against it
   instead of the local REPL, unless `SONDER_TERMINAL_REMOTE=0`.
5. Otherwise runs `python -m sonder_runtime repl` with your arguments.

`SONDER_TERMINAL_VERBOSE=1` shows the bootstrap output; `SONDER_BOOT_LOG`
names the capture file (default: a per-launch file in `%TEMP%`). Defaults set
when unset: `SONDER_CONTEXT_SIZE=8192`, `SONDER_NUM_THREAD=%NUMBER_OF_PROCESSORS%`,
`SONDER_NUM_GPU=999`, `SONDER_NUM_BATCH=512`, `OLLAMA_FLASH_ATTENTION=1` and
`SONDER_EXPOSE_REASONING=1` (surfaces reasoning a model emits on its own
thinking channel; set it to `0` before launching to turn it off) (`sonder.cmd:5-17`).
`sonder-serve.cmd`, `sonder-headless.cmd`, `bootstrap-engine.cmd` and
`endless-train.cmd` set the same thread/GPU/batch/flash-attention defaults.

### `sonder_headless.py` (`sonder-headless.*`)

`sonder-headless [start|engine|status|stop|restart] [--host H] [--port P]
[--stop-ollama] [--context-size N] [--allow-hosted]`
(`interfaces/cli/headless.py:40-54`).

| Flag | Default | Meaning |
|---|---|---|
| `--host` / `--port` | `SONDER_HOST` / `SONDER_PORT`, else `127.0.0.1` / `11435` | Address the managed server binds and `status` probes (`sonder_headless.py:30-31`). |
| `--stop-ollama` | off | With `stop`, also stop the Ollama it started. |
| `--context-size` | `SONDER_CONTEXT_SIZE` | Exported to the server as `SONDER_CONTEXT_SIZE`. |
| `--allow-hosted` | off | Sets `SONDER_ALLOW_CLOUD=1` for the server (cloud consent, `headless.py:55-60`). |

`start` starts Ollama if it is not reachable and local, makes sure the
`sonder:latest` alias exists (running `bootstrap_engine.py`, 30 minute limit),
then runs `python -m sonder_runtime serve <port>` detached and waits up to 12
seconds for the listener, then re-probes for the managed listener for up to 2
seconds more (`headless.py:102`). `engine` stops after the alias step. PID and log
files live in `<sonder-home>/run/` as `sonder_serve.pid|.log` and
`ollama.pid|.log` (`sonder_headless.py:53-56, 97-102`). `stop` sends SIGTERM and
waits up to 30 seconds on POSIX (it reports failure rather than escalating) and
runs `taskkill /T /F` on Windows. Exit 0 success. Exit 1 means a failed stop
(`stop`, or the stop step of `restart`), or a `start`/`restart` whose server
did not come up as a managed listener, including a port already held by an
`(unmanaged listener)` (`headless.py:73-76, 104`; `sonder_headless.py:553-558`;
`sonder.cmd` relies on this exit 1). Exit 2 means only a refused start
(`validate_start`) or an alias failure (`headless.py:77-87`). A server it did not start is reported as an
`unmanaged listener` and is never stopped.

### `sonder-launcher.*`

Binds `SONDER_LAUNCHER_HOST` (default `127.0.0.1`) and `SONDER_LAUNCHER_PORT`
(default `11436`) (`sonder-launcher.cmd:9-10`, `sonder-launcher.sh:9-10`);
exit 3 with no Python. Everything else is in [Mobile host control](../../MOBILE_HOST_CONTROL.md).

### `setup_alias.py`

Creates the `sonder:latest` alias over a base model and optionally an embedding
model (`setup_alias.py:196-311`). Run through `bootstrap_engine.py` or directly:
`python setup_alias.py [flags]`.

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `SONDER_BASE_MODEL`, else chosen from live host memory | Base Ollama model. |
| `--embed-model` | `SONDER_EMBED_MODEL`, else `nomic-embed-text` | Embedding model to pull/check. |
| `--no-embedding` | off | Skip the embedding model (semantic memory can be enabled later). |
| `--ollama` | auto-detect | Path to the `ollama` executable. |
| `--offline` | off | Use only models already known to the local Ollama server. |
| `--gguf PATH` | none | Import a local `.gguf` as the base model and alias it (implies offline). |
| `--from-usb` | off | Find a `.gguf` on mounted removable media; with several, re-run with `--gguf`. |
| `--usb-root PATH` | none | Extra mount path to scan; repeatable. |

Exit 0 done; 2 the base pull/import failed (or no/multiple USB candidates); 3
the alias could not be created; 4 blocked by the Ollama endpoint policy. A
failed embedding pull is a note, not a failure: chat works, recall and lessons
need the embedder later. Bootstrap models in the catalog are
`qwen2.5-coder:1.5b`, `qwen2.5-coder:3b` and `qwen2.5-coder:7b`
(`sonder_runtime/adapters/data/model_lists.json`); see
[Model catalog](18-model-catalog.md).

### `bootstrap_engine.py`

Detects RAM/GPU, picks a base model, makes sure the Python dependencies from
`requirements-runtime.txt` and a running Ollama are present, then calls
`setup_alias.py` (`bootstrap_engine.py:280-462`).

| Flag | Default | Meaning |
|---|---|---|
| `--dry-run` | off | Validate and print the plan; install and start nothing. |
| `--model` | `SONDER_BASE_MODEL`, else automatic | Override the base model (`auto` means automatic). |
| `--allow-cpu-offload` | on when `SONDER_ALLOW_CPU_OFFLOAD=1` | Allow a plan that spills to system RAM. |
| `--max-vram` | `SONDER_MAX_VRAM_GB` | Cap VRAM (GB) used for planning. |
| `--max-system-ram` | `SONDER_MAX_SYSTEM_RAM_GB` | Cap system RAM (GB) used for planning. |
| `--offline` | off (on when a bundle is present for models) | Never use pip or an Ollama registry. |
| `--bundle DIR` | auto-discovered | Directory containing `ENGINE-BUNDLE.json`. |

Exit codes: `0` ok; `2` Ollama could not be started; `3` Python dependencies
could not be installed or verified; `4` invalid bundle, model selection or
blocked Ollama endpoint; otherwise the exit code of `setup_alias.py`.
