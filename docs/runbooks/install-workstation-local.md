# Install: workstation-local profile

Target: a single owner's workstation. No reverse proxy needed because the
runtime only ever binds loopback in this profile.

On Windows, `packaging\install_workstation_local.ps1` automates steps 1 and 3
below in one idempotent command:

```powershell
git clone <your-fork-or-release> Sonder-runtime; Set-Location Sonder-runtime
powershell -NoProfile -File packaging\install_workstation_local.ps1
```

It creates `venv\`, installs `requirements-runtime.txt`, optionally creates
the `sonder:latest` Ollama alias (`-SkipModelAlias` to skip), then runs
`preflight --skip-ollama` and `migrate`. Reruns reuse the existing venv
unless `-Force` is passed. Every step is also documented below in case the
script is unavailable or a manual run is preferred.

## 1. Check out and prepare

```bash
# Linux/macOS
git clone <your-fork-or-release> Sonder-runtime && cd Sonder-runtime
python3 -m venv venv && ./venv/bin/pip install -r requirements-runtime.txt
```

```powershell
# Windows PowerShell
git clone <your-fork-or-release> Sonder-runtime; Set-Location Sonder-runtime
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements-runtime.txt
```

## 2. Optional configuration

The defaults (loopback, `workstation-local` profile) work without any
files. To customize, copy `packaging/sonder.toml.example`, set
`profile = "workstation-local"`, and pass `--config`.

## 3. Validate and run

```bash
# Linux/macOS
./venv/bin/python -m sonder_runtime preflight --skip-ollama
./venv/bin/python -m sonder_runtime migrate
./venv/bin/python -m sonder_runtime serve        # or: repl / mcp
```

```powershell
# Windows PowerShell
.\venv\Scripts\python.exe -m sonder_runtime preflight --skip-ollama
.\venv\Scripts\python.exe -m sonder_runtime migrate
.\venv\Scripts\python.exe -m sonder_runtime serve   # or: repl / mcp
```

`serve` refuses to start with a failed preflight and applies migrations
before opening the listener. State lives under the platform default
SONDER_HOME (`~/.local/share/sonder` on Linux, `%LOCALAPPDATA%\sonder` on
Windows); override with the `SONDER_HOME` environment variable.

## 4. Backups on a workstation

```bash
# Linux/macOS
./venv/bin/python -m sonder_runtime backup create --target ~/sonder-backups
./venv/bin/python -m sonder_runtime restore smoke ~/sonder-backups/<newest>
```

```powershell
# Windows PowerShell
.\venv\Scripts\python.exe -m sonder_runtime backup create --target $HOME\sonder-backups
.\venv\Scripts\python.exe -m sonder_runtime restore smoke $HOME\sonder-backups\<newest>
```

Schedule both with your platform scheduler (cron/Task Scheduler); the
server-private systemd timers in `packaging/systemd/` are the reference.

## 5. Before proposing a release tag

Run [release-smoke-check](release-smoke-check.md)
(`scripts/release_smoke.ps1` / `scripts/release_smoke.sh`) — it chains the
version-policy check with a real end-to-end smoke run in one command.
