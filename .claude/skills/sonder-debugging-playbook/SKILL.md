---
name: sonder-debugging-playbook
description: >-
  Symptom-to-fix triage for the Sonder Runtime: ranked causes plus the cheapest
  discriminating experiment for each known failure mode. TRIGGER when "tests fail",
  "connection refused", "server won't start", "Ollama unreachable", "doctor shows fail",
  "database is locked", "ModuleNotFoundError", or "runtime won't import after
  selfmod". DO NOT TRIGGER for chronological when-did-this-break investigation
  (use sonder-failure-archaeology — history mining stays there, never merged here),
  for operating the selfmod pipeline itself (use sonder-selfmod-lifecycle), or for
  layering and import-boundary design (use sonder-architecture-contract).
---

# Sonder debugging playbook

Symptom-first triage for the Sonder Runtime (Python 3.12, local-first AI runtime
around Ollama). Three ports matter everywhere below: **11434** = Ollama,
**11435** = the Sonder HTTP API, **11436** = the launcher control plane.
"State home" = the runtime's writable data directory; resolution details are
owned by `sonder-run-and-operate`. Override precedence is **layer-dependent**:
the raw path helper prefers `SONDER_STATE_HOME`
(`sonder_runtime/platform/paths.py:147-149`, `default_home`), while the typed
startup config prefers `SONDER_HOME`
(`sonder_runtime/platform/config.py:373`). If both are set to different
directories the two layers disagree — set exactly one; the alias trap is
documented in `sonder-config-and-flags`.

Each section gives: likely causes **ranked**, the **discriminating
experiment** (the cheapest command that splits hypothesis A from B, with what
each outcome means), and the fix or runbook pointer. Runbooks live in
`docs/runbooks/`.

## Symptom index

| Symptom | Section |
|---|---|
| Test fails with connection refused / port 1 | [1](#1-tests-hit-connection-refused-or-port-1) |
| `ModuleNotFoundError` on import | [2](#2-modulenotfounderror--import-failures) |
| Tests silently pass / too few tests ran | [3](#3-tests-pass-suspiciously-or-too-few-ran) |
| `scripts\run-tests.cmd` exits 3 or 4 | [4](#4-run-testscmd-exits-3-or-4) |
| Server won't start | [5](#5-server-wont-start) |
| `doctor` shows fail or warn | [6](#6-doctor-shows-fail-or-warn) |
| Ollama unreachable / no models / wrong model | [7](#7-ollama-unreachable-missing-models-routing-oddities) |
| "database is locked" / corruption | [8](#8-database-locked-or-corrupt) |
| Disk full / write failures | [9](#9-disk-exhaustion) |
| Runtime broken after a selfmod deploy | [10](#10-runtime-broken-after-selfmod) |
| Autopilot/selfmod run interrupted | [11](#11-interrupted-autopilot-or-selfmod-runs) |
| Mojibake / `UnicodeEncodeError` on Windows | [12](#12-windows-encoding-errors) |
| `check_architecture.py` exits 1 | [13](#13-architecture-gate-failures) |

## First three commands

Before deep triage, run these — they resolve or localize most incidents:

```
python -m sonder_runtime doctor
python -m sonder_runtime preflight
sonder-headless-status.cmd        # Windows; shows managed PIDs + log paths
```

`doctor` is read-only and exits 1 only on a hard fail (warn exits 0).
`preflight` runs the startup checks without binding a port. `status` prints
the managed process states and the exact log file paths under
`<state home>/run/`.

---

## 1. Tests hit connection refused or port 1

**Symptom**: a test fails (or you see in logs during pytest) a connection
error to `127.0.0.1:1` — for `OLLAMA_HOST`, `SONDER_SERVER`, or an embed call.

**This is the harness working as designed, not a service outage.** The root
`conftest.py` force-sets offline sentinels **at import time**, before
collection — `OLLAMA_HOST` and `SONDER_SERVER` point at port 1, cloud and
embed-cache legs are disabled, and state goes to a throwaway temp home. The
full sentinel table is owned by `sonder-validation-and-qa` §2.

Port 1 is unbindable on every OS, so a refusal is immediate and deterministic.

**Discriminating experiment** — is the failure "test needs a live service" vs
"test has a real bug"?

```
python -m pytest tests/test_<name>.py -x -q
```

- Failure is a connection error to **port 1** → the code under test reached
  for a live service it should have mocked. Fix the test's mocking, or mark it
  `network`/`model` (see section 3). Do not start Ollama to "fix" it.
- Failure is a connection error to **11434/11435** → something bypassed the
  sentinel (e.g. code reading its own hardcoded default). That is a product
  bug: config resolution ignored `OLLAMA_HOST`/`SONDER_SERVER`.

**Never** export `OLLAMA_HOST` inside a test session expecting real Ollama —
opt in with `--run-network` / `--run-model` instead (section 3).

## 2. `ModuleNotFoundError` / import failures

### `ModuleNotFoundError: No module named 'mcp.server.mcpserver'`

MCP **1.x is installed** but Sonder requires 2.x. `requirements-runtime.txt`
pins `mcp==2.0.0`; MCP 2.x renamed `mcp.server.fastmcp` → `mcp.server.mcpserver`
and `FastMCP` → `MCPServer`, and Sonder imports only the 2.x names. Full
background: `docs/MCP_2_MIGRATION.md`.

**Discriminating experiment**:

```
python -c "import mcp; print(mcp.__version__)"
```

- Prints `1.*` → `pip install -r requirements-runtime.txt` in the venv you
  actually run with (check which one: `python -c "import sys; print(sys.executable)"`).
- Prints `2.0.0` but the import still fails → wrong interpreter is running the
  code (venv mismatch); compare `sys.executable` between the failing process
  and your pip.

### `ModuleNotFoundError: No module named 'server'` (or any root module)

Sonder is **not an installed package**. `import server` works only when the
repo root is on `sys.path` — pytest gets this from `conftest.py`
(`sys.path.insert(0, repo_root)`), which only loads when pytest runs **from
the repo root**.

**Discriminating experiment**: `cd` to the repo root and re-run. Works there →
you were running from a subdirectory or with a bare interpreter; always launch
pytest and ad-hoc scripts from the root. Still fails at root → missing
dependency (see MCP case above) or a genuinely deleted module.

## 3. Tests pass suspiciously, or too few ran

Two silent-skip mechanisms exist. A check that stops checking looks identical
to a check that passes — always confirm what actually ran.

### Marked tests skip without opt-in

Tests marked `network` or `model` (markers declared in `pytest.ini`) are
skipped unless you pass `--run-network` / `--run-model`:

```
python -m pytest tests/ -q --run-network --run-model
```

**Discriminating experiment**: add `-rs` to see skip reasons. Skips reading
`requires explicit --run-network opt-in` mean the suite deliberately excluded
them — a green run without those flags proves nothing about network/model
paths.

### Regression selector returns nothing

`scripts/select_regression_tests.py` derives the test set from your diff's
changed identifiers.

```
python scripts/select_regression_tests.py                # working tree vs HEAD
python scripts/select_regression_tests.py --since main   # a whole branch
python scripts/select_regression_tests.py --format args  # paste into pytest
```

**Exit 2 = vacuous selection** (no identifiers extracted, or no tests
selected). Per its own docstring this is **an infrastructure failure, never
"nothing to run"**. Do not proceed on exit 2; also read its "uncovered"
report — a large selected set means nothing if your specific change is in the
uncovered list.

## 4. `run-tests.cmd` exits 3 or 4

`scripts\run-tests.cmd` is the robust pytest entrypoint; the quoting-failure
backstory and full exit-code narrative are owned by `sonder-build-and-env`.
The triage table:

| Exit | Meaning | Fix |
|---|---|---|
| 3 | no interpreter at `venv\Scripts\python.exe` and `SONDER_PYTHON` unset | `python -m venv venv` then `venv\Scripts\python.exe -m pip install -r requirements-dev.txt` |
| 4 | interpreter exists but won't start (`python -c "import sys"` failed) | its base Python (named in `venv\pyvenv.cfg`) moved or is unreadable; recreate the venv |
| other | pytest's own exit code | normal test triage |

**Discriminating experiment** for exit 4:

```
venv\Scripts\python.exe -c "import sys; print(sys.version)"
```

Error mentioning a path with a stray embedded quote (`No Python at '"...'`) is
the shell-boundary quoting failure — use `run-tests.cmd`, never re-quote the
interpreter path yourself.

## 5. Server won't start

`python -m sonder_runtime serve` runs preflight first and **fails closed**.
Read its output — each failing check is named. The preflight checks: state
home write probe, workspace roots writable, free disk ≥ 5 GiB
(`minimum_free_disk_bytes` default `5_368_709_120`, configurable), per-store
schema versions (a **future or modified** schema fails — a newer build touched
this state home), runtime policy loads, and optionally Ollama `/api/tags`.

### `MigrationRequired` / epoch-2 message

Serve requires an epoch-2 state home. If it exits telling you to
`` run `migrate --adopt-epoch2` ``:

```
python -m sonder_runtime migrate --adopt-epoch2
```

This migrates all stores and verifies cleanup; then retry `serve`.

### Preflight fails and you need the process up for recovery work

`--skip-preflight` exists on `serve` **for recovery only** — the CLI itself
warns "use --skip-preflight only for recovery work". Never leave it in a
launcher script.

### Port 11435 occupied by something that is not (provably) Sonder

Two surfaces report this, with different vocabularies. The **launcher
supervisor** probes the health endpoint with a nonce and validates an
HMAC-proved payload; a listener on 11435 that answers but **fails the proof**
is classified `foreign_listener` (`sonder_launcher.py:1315-1338`), and
start/restart refuse to touch it. The **headless** scripts do a simpler
managed-PID check and never print `foreign_listener`.

**Discriminating experiment** — dead port vs foreign process vs stale Sonder:

```
sonder-headless-status.cmd
```

Its `sonder api:` line reads one of (`sonder_headless.py:540-550`):

- `not listening` → nothing owns the port; plain start
  (`sonder-headless-start.cmd`).
- `listening on http://...:11435 (pid N)` → a managed Sonder server is up;
  your client is the problem.
- `unmanaged listener on http://...:11435 (not Sonder)` → find the owner:
  `netstat -ano | findstr :11435` (PowerShell/cmd) then
  `tasklist /fi "pid eq <PID>"`. Kill or reconfigure the foreign process; do
  not force-start Sonder onto the port. (`sonder-headless-start.cmd` also
  refuses here: "already listening ... (unmanaged listener)" is counted as a
  failed start, `sonder_headless.py:433-434`, `531-537`.)

PIDs and logs for managed processes live under `<state home>/run/`:
`sonder_serve.pid`, `sonder_serve.log`, `ollama.pid`, `ollama.log`. Read the
log before restarting — a crash loop restarted blind stays a crash loop.
Graceful stop/drain procedure: `docs/runbooks/start-stop-drain.md`
(`python -m sonder_runtime drain` requests a drain of a running server).

### Cheap end-to-end confidence check

```
python -m sonder_runtime smoke
```

Runs preflight plus an operations-store write/read roundtrip in the real state
home and prints `smoke passed` on success — no model required
(`--skip-ollama` skips the Ollama probe).

## 6. `doctor` shows fail or warn

```
python -m sonder_runtime doctor            # add --json for structured output
python -m sonder_runtime doctor --storage-probe   # adds 8 MiB / 5 s throughput probe
```

Exit code is 1 **only on FAIL**. The full check anatomy (order, output
format, per-check detail strings) is owned by
`sonder-diagnostics-and-tooling`; the first-move triage rows:

| Check | Fails/warns when | First move |
|---|---|---|
| `config` | config file invalid or unreadable | read the detail; fix the named key |
| `storage_state` | state-home root problems (space, writability) | section 9 if disk; permissions otherwise |
| `storage_models` | model storage roots problems | same |
| `schemas` | store schema mismatch | `python -m sonder_runtime migrate` (or `--adopt-epoch2`, section 5) |
| `self_heal` | self-heal subsystem unhealthy | read detail; `skip` = module unavailable, not failure |
| `memory_quality` | memory audit findings | read detail |
| `runtime_policy` | policy file won't load | `/runtime reset` via the API, or inspect the policy detail |
| `ollama` | endpoint unreachable (FAIL) or reachable with zero models (**warn**) | section 7 |

Key discrimination built into doctor: **Ollama reachable with an empty model
catalog is a warn, not ok** — "reachable is not ready". Its own detail tells
you the fix: `run setup_alias.py`.

## 7. Ollama unreachable, missing models, routing oddities

Design fact first: **missing models degrade, they do not crash.** An unbound
reasoning/vision tier simply is not offered; a missing or incompatible
embedding model disables semantic recall while chat and lexical retrieval
continue (the retriever explicitly treats a vector from a different model as
"no compatible semantic corpus" and falls back to lexical). So "the runtime is
up but answers feel dumber / recall is worse" is a model-inventory symptom,
not a crash symptom.

**Discriminating experiment** — provider state vs Sonder routing state:

```
ollama list                      # what the provider actually has
ollama show sonder:latest        # does the stable alias exist?
```

then compare against Sonder's view via the API/REPL slash command
`/runtime` (alias `/models`), which shows tier→model bindings, lane→tier
routing, and readiness.

- `ollama list` empty or missing `sonder:latest` → provider problem:
  `python setup_alias.py` rebuilds the stable alias (`sonder:latest`).
- Provider has the models but `/runtime` shows a tier unbound → policy
  problem: `/runtime set code=<model> reasoning=<model> embedding=<model>` or
  `/runtime reset`.
- Ollama endpoint itself down → `curl http://127.0.0.1:11434/api/tags`.
  Connection refused = Ollama not running (`sonder-headless-start.cmd` manages
  it; log at `<state home>/run/ollama.log`). During an outage the HTTP API
  keeps `/live` = 200 while `/ready` = 503 — **that split is correct
  behavior**; do not restart Sonder to fix a red readiness check. Full
  procedure: `docs/runbooks/ollama-outage.md`.

**Remote Ollama rejected**: a non-loopback `OLLAMA_HOST` is refused unless
`SONDER_ALLOW_REMOTE_OLLAMA=1` (fail-closed policy in
`sonder_runtime/domain/ollama_policy.py`; default host `127.0.0.1:11434`;
`localhost`/`0.0.0.0`/`::` are rewritten to loopback). Same opt-in gates
remote worker endpoints in the multi-PC pool — see
`docs/runbooks/multi-pc-ollama.md`.

## 8. Database locked or corrupt

Stores under the state home: `memory.db`, `autopilot.db`, `fleet.db`,
`operations.db`, `queued_actions.db`, `updates.db`, `jobs.db`; selfmod state
under `<state home>/selfmod/` (`selfmod.db`, `backups/`, `workspaces/`).

Full runbook: `docs/runbooks/database-lock-or-corruption.md`. Summary:

- **Lock storm (`database is locked` / SQLITE_BUSY)**: identify the store from
  log context, look for a process holding a long transaction, then drain +
  restart to clear in-process contention. Persistent contention is a bug —
  capture evidence and file it; the runbook explicitly forbids raising
  `busy_timeout` past 30 s as a fix.
- **Corruption (`database disk image is malformed`)**: stop the service first,
  then confirm with `sqlite3 <state home>/<store>.db "PRAGMA integrity_check;"`,
  then restore per `docs/runbooks/backup-restore.md`.

**Discriminating experiment** — lock vs corruption: run the
`PRAGMA integrity_check` above on a **stopped** service. `ok` = contention
problem (restart clears it); anything else = corruption (restore path).

## 9. Disk exhaustion

Preflight fails closed below 5 GiB free on the state home, so "server won't
start + disk_space check failing" usually reveals this before writes corrupt
anything. `doctor`'s `storage_state` check warns earlier. Follow
`docs/runbooks/disk-exhaustion.md` for what is safe to delete (model blobs,
old backups) versus what is state.

**Discriminating experiment** — Sonder's data vs something else filling the
volume: check the state home's own size first (PowerShell:
`Get-ChildItem $env:LOCALAPPDATA\sonder -Recurse | Measure-Object Length -Sum`).
Small state home on a full disk = the problem is elsewhere on the volume.

## 10. Runtime broken after selfmod

**Symptom**: the runtime cannot import or start immediately after a selfmod
deploy. This is the one scenario where normal tooling may itself be broken, so
the recovery tool is **stdlib-only and imports no Sonder module by design**:

```
python selfmod_recover.py <state home>/selfmod/backups/<run-id>/manifest.json
```

(Windows example from `SELFMOD.md`:
`py selfmod_recover.py %LOCALAPPDATA%\sonder\selfmod\backups\<run-id>\manifest.json`.)

Backups live at `<state home>/selfmod/backups/<run-id>/` with `manifest.json`,
`manifest.sha256`, and the pre-deploy `files/`. Pick the run-id of the deploy
that broke things (newest directory, usually). After restore, verify with
`python -m sonder_runtime smoke`.

Everything about operating selfmod normally — enabling, gating, resume,
promotion — belongs to the **sonder-selfmod-lifecycle** skill; this section is
only the emergency exit.

## 11. Interrupted autopilot or selfmod runs

Unclean stops (crash, kill, reboot) leave runs in a claimed state. On startup
the claim reaper marks that work `interrupted`. Nothing resumes implicitly:

- Autopilot: `/autopilot resume <id>` (or cancel). Full procedure:
  `docs/runbooks/autopilot-interruption.md`.
- Selfmod: interrupted runs **never resume without** `/selfmod resume <run-id>`
  (per `SELFMOD.md`); inspect first with `/selfmod status`.

**Discriminating experiment** — hung vs interrupted: `/autopilot` (status)
shows the run state. `interrupted` = reaped, safe to resume/cancel; still
`running` with no progress = live-hang, capture logs from
`<state home>/run/sonder_serve.log` before restarting.

## 12. Windows encoding errors

Mojibake or `UnicodeEncodeError`/`UnicodeDecodeError` around subprocess output
on Windows consoles is a **known recurring class** here, fixed repeatedly by
forcing utf-8 on child-process lanes (commits `67ca41a4` "Force utf-8 output
in the run_program lane too", `7f34f6fd` "Decode generated-code output as the
utf-8 the child is told to emit").

**Discriminating experiment** — console codepage vs missing utf-8 forcing in a
lane:

```
python -X utf8 <failing entrypoint>
```

- Fixed by `-X utf8` → the lane spawning/decoding the child is not forcing
  utf-8; fix the lane the way the two commits above did (tell the child to
  emit utf-8 **and** decode as utf-8), don't rely on user environment.
- Not fixed → the bytes themselves are not utf-8 (binary output, or a tool
  emitting the OEM codepage); handle at the producer.

Context: development happens on Windows + WSL + NVIDIA; macOS, AMD, and
CPU-only paths are the least exercised (per `CONTRIBUTING.md`), so
platform-conditional bugs cluster there.

## 13. Architecture gate failures

```
python scripts/check_architecture.py
```

Exit 0 with no output = architecture holds. Exit 1 lists **one violation per
line**. It enforces (SPEC-3 §12): layer dependency direction inside
`sonder_runtime/` (domain → stdlib only; application → domain; adapters →
domain/application/platform; etc.), no internal import cycles, no
`sqlite3.connect` / `subprocess` / `urllib`/`socket`/`http.client` outside
adapters, and no `os.environ` reads in domain or application modules.

Common causes, ranked: (1) a new import edge violating the layer table,
(2) sqlite/subprocess/network use outside adapters, (3) `os.environ` in
domain/application, (4) a production caller importing a compatibility root
module or a reintroduced retired path.

**Fix by moving code to the right layer — never by widening the allowlists**
in the script. The layer rules and where each concern belongs are the
**sonder-architecture-contract** skill's territory; use it before
restructuring.

## When NOT to use this skill

- "When did this break / which commit introduced it / has this failed before"
  → **sonder-failure-archaeology** (chronological history mining lives there;
  the two are deliberately never merged).
- Operating or reasoning about the selfmod pipeline beyond the emergency
  restore in section 10 → **sonder-selfmod-lifecycle**.
- Deciding where code belongs, layering, import boundaries →
  **sonder-architecture-contract**.
- Pre-commit/PR gating, baselines, release mechanics → **sonder-change-control**.

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Re-verify volatile claims with:

- Offline sentinels and skip markers: `grep -n "127.0.0.1:1\|run-network\|run-model" conftest.py`
- run-tests exit codes: `grep -n "exit /b" scripts/run-tests.cmd` (expect 3 and 4)
- Selector vacuous exit: `grep -n "Exit codes" -A 2 scripts/select_regression_tests.py`
- MCP pin: `grep mcp== requirements-runtime.txt` (expect `mcp==2.0.0`)
- CLI surface: `python -m sonder_runtime --help` (serve, preflight, doctor, migrate, drain, smoke present)
- Epoch-2 flag: `grep -n "adopt-epoch2" sonder_runtime/__main__.py`
- Doctor check order: `grep -n -A 12 "def default_checks" sonder_doctor.py`
- Zero-models warn: `grep -n "reachable, no models" sonder_doctor.py`
- Preflight disk floor: `grep -n "minimum_free_disk_bytes" sonder_runtime/platform/config.py` (expect `5_368_709_120`)
- foreign_listener logic: `grep -n "_server_state" sonder_launcher.py`
- Run-dir names: `grep -n "run_dir\|pid_file\|log_file" sonder_headless.py`
- State-home resolution: `grep -n -A 30 "def default_home" sonder_runtime/platform/paths.py`
- Loopback policy: `grep -n "REMOTE_OPT_IN\|DEFAULT_HOST" sonder_runtime/domain/ollama_policy.py`
- Recovery tool is stdlib-only: `head -1 selfmod_recover.py`
- Backup layout: `grep -n "backups/" SELFMOD.md`
- Architecture gate rules: `head -20 scripts/check_architecture.py`
- Ports: `grep -n "DEFAULT_PORT\|SERVER_PORT" sonder_launcher.py` (11436 / 11435)
- Runbook set: `ls docs/runbooks/`
