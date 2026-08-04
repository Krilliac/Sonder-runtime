# Install: workstation-local profile

Target: a single owner's workstation. No reverse proxy needed because the
runtime only ever binds loopback in this profile.

## 1. Check out and prepare

```bash
git clone <your-fork-or-release> Sonder-runtime && cd Sonder-runtime
python3 -m venv venv && ./venv/bin/pip install -r requirements-runtime.txt
```

## 2. Optional configuration

The defaults (loopback, `workstation-local` profile) work without any
files. To customize, copy `packaging/sonder.toml.example`, set
`profile = "workstation-local"`, and pass `--config`.

## 3. Validate and run

```bash
./venv/bin/python -m sonder_runtime preflight --skip-ollama
./venv/bin/python -m sonder_runtime migrate
./venv/bin/python -m sonder_runtime serve        # or: repl / mcp
```

`serve` refuses to start with a failed preflight and applies migrations
before opening the listener. State lives under the platform default
SONDER_HOME (`~/.local/share/sonder` on Linux); override with the
`SONDER_HOME` environment variable.

## 4. Backups on a workstation

```bash
./venv/bin/python -m sonder_runtime backup create --target ~/sonder-backups
./venv/bin/python -m sonder_runtime restore smoke ~/sonder-backups/<newest>
```

Schedule both with your platform scheduler (cron/Task Scheduler); the
server-private systemd timers in `packaging/systemd/` are the reference.
