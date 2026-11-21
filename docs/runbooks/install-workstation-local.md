# Install: workstation-local profile

Target: a single owner's workstation, installed from a source checkout or an
extracted local-system bundle, on Windows, Linux, macOS or WSL2. No reverse
proxy is needed because the server binds loopback only in this profile
(`127.0.0.1:11435` by default). For a systemd service behind TLS use
[install-server-private](install-server-private.md); the ordered path through
every setup page is [setup-start-here](setup-start-here.md).

This page is the one home for from-source and bundle installs (checked
2026-11-20).

## Prerequisites

- Python 3.11 or later. The installer script rejects anything older
  (`packaging/install_workstation_local.ps1:154-157`). CI on `main` runs 3.14
  (`.github/workflows/ci.yml:38,231,275`); 3.12 runs in a non-required
  compatibility job that the workflow comment says stays "for one release"
  after the move to 3.14 (`ci.yml:185-195`); 3.11 is an untested floor.
- Git, to clone the repository.
- [Ollama](https://ollama.com) installed and running, plus one generative
  model (see
  [Model Requirements & Onboarding](../wiki/19-model-requirements-and-onboarding.md)).
  `preflight --skip-ollama` and `migrate` need no model, but `serve` does
  probe Ollama (see [Failure modes](#failure-modes)).

## Installer script (`install_workstation_local.ps1`)

Windows only; there is no Linux/macOS equivalent script (use
[Manual install](#manual-install)). Run it from a clone:

```powershell
git clone <your-fork-or-release> Sonder-runtime; Set-Location Sonder-runtime
powershell -NoProfile -File packaging\install_workstation_local.ps1
```

Parameters (`install_workstation_local.ps1:58-65`):

| Parameter | Effect |
|---|---|
| `-Python <path>` | Interpreter used to build the venv. Default: the `py -3` launcher, then `python` on `PATH` (`:132-148`). |
| `-VenvPath <path>` | Venv location. Default: `<repo>\venv`, which is where the launcher scripts look. |
| `-Force` | Delete and recreate an existing venv. Refused unless the target is a real venv (`pyvenv.cfg` present; the default `venv\` is exempt so a half-created one can be rebuilt), and always refused for the checkout, any folder containing it, drive roots, profile/system folders and links. |
| `-ManagedRuntime` | Also build `venv-managed\` (see below). Requires Windows CPython 3.12 or 3.14 (`:158-162`). |
| `-SkipModelAlias` | Skip the `sonder:latest` alias step. |
| `-BaseModel <tag>` | Base Ollama model for the alias; forwarded to `setup_alias.py --model`. Default: `setup_alias.py` chooses from live host memory. |

What it does, in order: find an interpreter and check it is 3.11+; create the
venv (an existing one is reused unless `-Force`); upgrade pip with
`python -m pip`; install `requirements-runtime.txt`; run `pip check`
(`requirements-runtime.txt` pins only top-level releases and transitive
dependencies resolve at install time without hashes, so an inconsistent
resolution fails the install); optionally provision `venv-managed\`; run
`setup_alias.py` (a failure is a warning, not an error: start Ollama and rerun
it); run `preflight --skip-ollama`; run `migrate`. Each step prints
`[sonder] ...`; any non-alias failure stops the script with the failing command
line.

Observe: it ends with `Install complete.` and a "Next steps" block.

Note: the trailing `migrate` step stamps a brand-new state home at epoch 2
itself (see [Fresh state home and the epoch-2 gate](#fresh-state-home-and-the-epoch-2-gate)),
so the "Next steps" `serve` starts directly. Run
`python -m sonder_runtime migrate --adopt-epoch2` (with the venv's Python)
only when the script ran against an existing pre-epoch-2 state home; it is
idempotent.

If PowerShell reports `running scripts is disabled on this system`, the default
client execution policy blocked the file. Do not use a Bypass policy. Either
allow locally created and signed scripts for your user only
(`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`), or follow
[Manual install](#manual-install). Files downloaded as a zip may also need
`Unblock-File packaging\install_workstation_local.ps1`.

For the opt-in Windows foreground `ManagedRuntimeOwner`, provision a separate
interpreter closure:

```powershell
powershell -NoProfile -File packaging\install_workstation_local.ps1 -ManagedRuntime -SkipModelAlias
```

This installs the same pinned `requirements-runtime.txt` into a new
`venv-managed\` without training or development packages, then seals the base
interpreter, disabled system-site policy, checkout requirements and installed
distributions (`:199-221`). Reruns verify the profile
instead of silently adopting new packages; `-Force` recreates it. From
host-owned Python code, select it with
`ManagedRuntimeOwner.workstation_local(owner_path, writable_roots=live_roots)`,
or pass its **absolute** path as `runtime_venv=` to `ManagedRuntimeOwner`. Keep
the profile root outside model-writable roots. The runtime hashes the full
selected closure at creation, before native spawn, and in the child; a host
using a training-laden `venv\` stays available through the ordinary
constructor but keeps that closure's verification cost.

Rollback: delete the venv directory (`venv\`, `venv-managed\`); nothing else
outside the state home was modified (the script never edits `PATH`, installs a
service or touches Git history).

## Manual install

Use this on Linux and macOS, or on Windows when the script is unavailable.

```bash
# Linux/macOS
git clone <your-fork-or-release> Sonder-runtime && cd Sonder-runtime
python3 -m venv venv
./venv/bin/python -m pip install -r requirements-runtime.txt
./venv/bin/python -m pip check
./venv/bin/python setup_alias.py          # needs Ollama running
```

```powershell
# Windows PowerShell
git clone <your-fork-or-release> Sonder-runtime; Set-Location Sonder-runtime
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
.\venv\Scripts\python.exe -m pip check
.\venv\Scripts\python.exe setup_alias.py   # needs Ollama running
```

On Debian and Ubuntu, `python3 -m venv venv` fails with `ensurepip is not
available` until you install the distribution's venv package
(`sudo apt install python3-venv`); delete the half-created `venv` folder and
rerun.

Observe: `pip check` prints `No broken requirements found.`. Use
`python -m pip`, not `pip.exe install --upgrade pip`, on Windows: a running
`pip.exe` cannot replace itself.

`setup_alias.py` creates the `sonder:latest` Ollama alias and also tries to
pull the `nomic-embed-text` embedding model; without the embedding model chat
works but semantic recall is unavailable. Its flags (`--model`,
`--embed-model`, `--no-embedding`, `--ollama`, `--offline`, `--gguf`,
`--from-usb`, `--usb-root`; `setup_alias.py:195-235`) are documented in
[CLI reference: launchers and setup scripts](../wiki/04-cli-and-entrypoint.md#launchers-and-setup-scripts).

Optional configuration: the defaults (loopback, `workstation-local` profile)
need no files. To customise, copy `packaging/sonder.toml.example`, set
`profile = "workstation-local"` and pass `--config`. Without flags the runtime
picks up `sonder.toml` and `sonder.env` from the state home if they exist, or
the paths in `SONDER_CONFIG` / `SONDER_SECRETS`; an explicitly given path that
does not exist is an error (`sonder_runtime/__main__.py:75-88`,
`sonder_runtime/platform/config.py:2455,2516-2517`).

## Optional dependency sets

`requirements-runtime.txt` is the only file the installers use. Add others
into the same venv only when you need the feature (pins re-read 2026-11-20).

| File | Contents | Install only when |
|---|---|---|
| `requirements-runtime.txt` | `mcp==2.2.0`, `cryptography==50.0.0`, `pywin32==312` (Windows only) | Always. |
| `requirements-dev.txt` | `-r requirements-runtime.txt` plus pytest, pytest-xdist, ruff, mypy and test tooling | You run the test suite or gates ([CONTRIBUTING](../../CONTRIBUTING.md)). |
| `requirements-update.txt` | `tuf==7.0.0`, `securesystemslib==1.4.0`, `cryptography==50.0.0` | You verify TUF-signed update bundles or publish them ([update manager](../wiki/13-update-manager.md)). |
| `requirements-postgres.txt` | `psycopg[binary]==3.3.5`, `psycopg-pool==3.3.0` | You use the optional child-aggregate backend; SQLite installs do not need it. |
| `requirements-anthropic.txt` | `anthropic>=1.1.0,<2` | A tier is bound to the Anthropic provider ([anthropic](../integration/anthropic.md)). |
| `requirements-train.txt` | Training stack; `torch` comes from a CUDA index first | You run adapter training; follow [TRAINING.md](../../TRAINING.md) before installing. |

Install one with `./venv/bin/python -m pip install -r requirements-update.txt`
(Windows: `.\venv\Scripts\python.exe -m pip ...`).

## Fresh state home and the epoch-2 gate

Mechanism (`sonder_runtime/adapters/persistence/sqlite/bridge_migration.py:75-110`,
`sonder_runtime/__main__.py:1180-1206,1275-1291`): `serve`, `repl` and
`mcp --native` require every domain database to be at schema epoch 2.
`serve`, `migrate`, `repl` and `mcp` all create a brand-new home at epoch 2
(`stamp_fresh_home`, which also writes `epoch2_adoption_receipt.json`) before
they create stores (`__main__.py:650,1283,1431,1501,1539`). Only `smoke`
(`__main__.py:997`) creates `operations.db` through the ordinary migrations
without that stamp. `stamp_fresh_home` returns `False` as soon as any legacy
store exists, so a home that `smoke` touched first is no longer "fresh", and
the next `serve`, `repl` or `mcp --native` refuses it.

Symptom, on `serve` (exit 1):

```text
migration required before serve: <reason>; run `migrate --adopt-epoch2`
```

`doctor` shows `schema_epoch` as FAIL until the repair has run
([CLI reference](../wiki/04-cli-and-entrypoint.md)). It also shows FAIL
("serve will refuse to start") on a brand-new, untouched home, where `serve`
would stamp the home without complaint. That is a false alarm: do not run the
repair on a home that no command has touched yet; just start `serve`.

Rule: on a fresh home `serve`, `migrate`, `repl` and `mcp` each stamp epoch 2
automatically before they create stores, and `preflight` is safe before them
(it creates the empty home directory and no database stores). Do not run
`smoke` first on an untouched home: it migrates `operations.db` without the
stamp.

Repair when a home is already in the refused state (an existing pre-epoch-2
home, or one that `smoke` touched first):

```bash
./venv/bin/python -m sonder_runtime migrate --adopt-epoch2
./venv/bin/python -m sonder_runtime doctor --skip-ollama     # schema_epoch ok
```

```powershell
.\venv\Scripts\python.exe -m sonder_runtime migrate --adopt-epoch2
.\venv\Scripts\python.exe -m sonder_runtime doctor --skip-ollama
```

- It first copies the databases to `backups/pre-epoch2-*` under the state
  home, then adopts epoch 2 and verifies it (`__main__.py:628-649`).
- It is idempotent: on a home that already passes the adoption check it
  prints `already_adopted` and takes no new copy (`__main__.py:607-627`).
- `--store` cannot be combined with `--adopt-epoch2`: exit 2 and the message
  `--store cannot be combined with --adopt-epoch2` (`__main__.py:567-575`).
- If the home directory does not exist yet, the command exits 2 with
  `--adopt-epoch2: state home does not exist: <home>; nothing to adopt (a fresh
  home is stamped by serve)` (`__main__.py:592-599`). Run `serve`, `migrate`,
  `repl` or `mcp` instead, which create and stamp a fresh home.
- Rollback: restore the databases from the `backups/pre-epoch2-*` copy; see
  [backup-restore](backup-restore.md).

## Validate and run

Run `preflight`, then `serve` (`serve` runs the migrations itself; running
`migrate` beforehand is also safe on a fresh home).

```bash
# Linux/macOS
./venv/bin/python -m sonder_runtime preflight --skip-ollama
./venv/bin/python -m sonder_runtime serve
```

```powershell
# Windows PowerShell
.\venv\Scripts\python.exe -m sonder_runtime preflight --skip-ollama
.\venv\Scripts\python.exe -m sonder_runtime serve
```

Observe: `preflight` prints a report and exits 0 when every required check
passes (`__main__.py:391-401`). `serve` runs the full preflight (including the
Ollama probe), the epoch-2 gate and then migrations before the listener opens,
so no socket exists until all succeed (`__main__.py:1228-1305`, `sonder_runtime/adapters/preflight.py`). In another terminal:

```bash
curl http://127.0.0.1:11435/ready      # 503 until the Ollama probe succeeds, then 200
./venv/bin/python -m sonder_runtime doctor
```

(Windows: `.\venv\Scripts\python.exe -m sonder_runtime doctor`.) The server
re-probes Ollama every `max(1, startup_timeout/4)` seconds, which is 15 s with
the default `[ollama].startup_timeout_seconds = 60`
(`sonder_runtime/adapters/web/lifecycle.py:1347`, `platform/config.py:236`).
Change that setting to change the interval; the 15 s constant at
`lifecycle.py:392` is only the fallback when no interval is passed. Once `serve` has started
once, `./venv/bin/python -m sonder_runtime repl` (interactive terminal) and
`mcp` ([integrations](../../integrations/README.md)) are safe to use. Point
OpenAI-compatible UIs at `http://127.0.0.1:11435/v1`.

With no `SONDER_API_KEY` on a loopback bind, `api-key` auth mode is downgraded
to `local-open` and a WARNING is logged
(`sonder_runtime/interfaces/http/serve.py:1228-1230`). Setting a key, and what
changes with it, is covered in
[configure-authentication](configure-authentication.md).

Exit codes: see
[CLI reference](../wiki/04-cli-and-entrypoint.md#exit-codes).

## Extracted local-system bundle

The local-system archive is self-contained and the `.cmd` launchers do not
change your `PATH`. Run the launchers from its extracted directory. (Sourcing
`. ./sonder-runtime.sh` in a POSIX shell does prepend the bundled Ollama to
`PATH` when the bundle ships one, for that shell session only:
`sonder-runtime.sh:65-67,75`.)

```powershell
# Windows PowerShell
.\bootstrap-engine.cmd      # one-time engine and model setup
.\sonder.cmd
```

```bash
# Linux/macOS
./bootstrap-engine.sh
. ./sonder-runtime.sh
"$SONDER_PYTHON" -m sonder_runtime repl
```

Interpreter selection order, `SONDER_ENGINE_BUNDLE`, `SONDER_OLLAMA_EXE`, the
`sonder.cmd` steps, skip flags and defaults, and the launcher exit codes are
documented once in
[CLI reference: launchers and setup scripts](../wiki/04-cli-and-entrypoint.md#launchers-and-setup-scripts).
A POSIX bundle ships no bare `sonder` executable, so keep the explicit command
above unless you write a wrapper.

Epoch-2 note: both `serve` and `repl` stamp a brand-new state home at epoch 2
before they create stores (`__main__.py:1283,1431`), so `sonder.cmd` on Windows
and the explicit `repl` command on a POSIX bundle both leave a fresh home
stamped, even when no server has started. Only an existing pre-epoch-2 home (or
one touched first by `smoke`) needs `migrate --adopt-epoch2`; see
[Fresh state home and the epoch-2 gate](#fresh-state-home-and-the-epoch-2-gate).

## WSL2 (Linux inside Windows)

Sonder runs inside a WSL2 distribution like native Linux: follow the
Linux/macOS commands above inside the distribution. WSL has its **own** state
home (`~/.local/share/sonder` inside the distribution, separate from the
Windows `%LOCALAPPDATA%\sonder`), its own `sonder.toml`, and its own Ollama;
nothing is shared with a Windows install. Differences:

- **Resource limits.** Set `memory`, `processors` and `swap` under `[wsl2]` in
  `%USERPROFILE%\.wslconfig`.
- **systemd.** Needs this in `/etc/wsl.conf` inside the distribution, followed
  by `wsl --shutdown` from Windows:

  ```ini
  [boot]
  systemd=true
  ```

  Ollama and Sonder systemd units then work normally.
- **Port access from the Windows host.** WSL2 uses NAT. Forward ports with
  `netsh interface portproxy` if other machines must reach services inside WSL;
  the WSL2 address changes on reboot, so automate the rule in a startup task.
- **GPU.** WSL2 passes the host GPU through with an up-to-date Windows driver;
  verify with `ollama ps` after loading a model.

Using a WSL2 node as a remote Ollama worker:
[multi-node-ollama](multi-node-ollama.md).

## Backups on a workstation

Commands, verification and restore live in [backup-restore](backup-restore.md).
Workstation specifics:

- Schedule `backup create` with cron or Task Scheduler; the server-private
  systemd timers in `packaging/systemd/` are the reference.
- Backups are not encrypted: `SONDER_BACKUP_KEY_FILE` is refused with a
  configuration error (`sonder_runtime/platform/config.py:133-136,1783-1788`).
  Protect the backup target with filesystem permissions or an encrypted
  volume.
- Nightly learning and selfmod candidate branches on Windows have their own
  account and isolation requirements:
  [scheduled-task deployment guide](nightly-selfmod-windows.md).

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `PREFLIGHT FAIL: <name>: <detail>` then `refusing to bind with failed preflight`, exit 1 | A required check failed (`__main__.py:1228-1240`) | Fix the named check below, or use `--skip-preflight` for recovery work only. |
| `state_home_writable` fails | State home cannot be created or written (`sonder_runtime/adapters/preflight.py:23-35`) | Fix permissions or set `SONDER_HOME` to a writable folder. |
| `workspace_root:<path>` fails | A `[state].workspace_roots` entry is not an existing writable directory (`preflight.py:34-45`) | Create it or remove it from the config. |
| `disk_space` fails | Free space below `[state].minimum_free_disk_bytes`, default 5368709120 (5 GiB) (`preflight.py:49-60`, `platform/config.py:200`) | Free space or lower the setting. |
| `schema:<store>` fails with `future schema` or `migration history modified` | Database from a newer build, or edited migration files (`preflight.py:63-88`) | Use the matching release; do not edit migrations. A merely pending migration is not required and does not block. |
| `runtime_policy` fails | `runtime_policy.load()` raised, for example on a missing or invalid shipped `model_lists.json` or a lock timeout while creating the policy file (`preflight.py:91-104`). A corrupt `<state home>/runtime_policy.json` falls back to defaults with the error recorded and does not fail this check | Read the detail text; verify the shipped `sonder_runtime/adapters/data/model_lists.json` and lock contention under the state home. |
| `ollama` fails (`/api/tags` unreachable) | Ollama is down or the URL is wrong; required unless `--skip-ollama` (`preflight.py:137-147`) | Start Ollama; see [ollama-outage](ollama-outage.md). |
| `/ready` returns 503 | The Ollama probe has not yet succeeded (re-probe interval: see [Validate and run](#validate-and-run)) | Start Ollama; the probe retries on its own. |
| `migration required before serve` | Epoch-2 gate | See [Fresh state home and the epoch-2 gate](#fresh-state-home-and-the-epoch-2-gate). |
| `WARNING: local API server did not start cleanly.` from `sonder.cmd`, then the REPL opens | The background start failed (or another listener already held the port, which counts as already running) (`sonder.cmd:81`) | Read the startup log the script prints, fix the start problem, then `serve`. The REPL still stamps a fresh home at epoch 2 itself (`__main__.py:1431`). |
| `server cannot bind to 127.0.0.1:11435, port may already be in use`, then `OSError` | Another process (often another `serve`) holds the port (`sonder_runtime/interfaces/http/serve.py:8253-8257`) | Stop it or pass another port: `serve <port>` or `--set server.port=<port>`. |
| `secrets file <path> must not be group/world accessible` (POSIX) | `sonder.env` is readable by group or others (`platform/config.py:2522-2527`) | `chmod 600 <path>`. |
| `SONDER_BACKUP_KEY_FILE is not supported` | The variable is set to a non-empty value (`platform/config.py:1783-1788`) | Unset it. |
| `secrets file not found` or an explicit `--config` path error | An explicit path does not exist (`platform/config.py:2517-2519`) | Fix the path; only the default `<home>/sonder.toml` and `<home>/sonder.env` are optional. |

## State-home location

Defaults when nothing is set: `%LOCALAPPDATA%\sonder` on Windows,
`~/Library/Application Support/sonder` on macOS (an existing legacy
`~/.local/share/sonder` is kept until the native folder exists), and
`$XDG_DATA_HOME/sonder` or `~/.local/share/sonder` on Linux
(`sonder_runtime/platform/paths.py:170-194`).

To move it, set **`SONDER_HOME`** (one variable, in the process environment)
or `[state].home`. The environment overrides `[state].home`, and
`SONDER_STATE_HOME` beats `SONDER_HOME`, both under `serve` (typed
configuration) and in the commands that load no configuration
(`sonder_runtime/platform/paths.py:157-176`,
`sonder_runtime/platform/config.py:1597-1599`); see
[Configuration: State home](../wiki/03-configuration.md#state-home). Do not
set both environment variables to different values.

The launcher scripts compute `SONDER_HOME` themselves; see
[CLI reference: launchers and setup scripts](../wiki/04-cli-and-entrypoint.md#launchers-and-setup-scripts).
Precedence of configuration
sources is documented once, in [Configuration](../wiki/03-configuration.md).

## Before proposing a release tag

Run [release-smoke-check](release-smoke-check.md)
(`scripts/release_smoke.ps1` / `scripts/release_smoke.sh`); it chains the
version-policy check with a real end-to-end smoke run in one command.
