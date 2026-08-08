# Getting Started

Two supported profiles: **workstation-local** (your machine, loopback
only) and **server-private** (self-hosted Linux, systemd, TLS reverse
proxy for remote access). This page gets you running; the runbooks cover
production install in detail.

## Prerequisites

- Python 3.11+ (3.12 in CI).
- [Ollama](https://ollama.com) installed and running.
- A model. Either pull one online, or import a portable GGUF (see
  [Model Tiers & Gateway](08-model-tiers-and-gateway.md) and the
  [facts. runbook](../runbooks/use-facts-model.md)).
- RAM: 8 GB minimum for a 4B model; more for 7B+.

## Workstation quickstart (Linux/macOS)

```bash
git clone <your-fork> Sonder-runtime && cd Sonder-runtime
python3 -m venv venv
./venv/bin/pip install -r requirements-runtime.txt

# Create the sonder:latest alias from a coder model (online):
./venv/bin/python setup_alias.py
# ...or import a portable GGUF offline:
./venv/bin/python setup_alias.py --from-usb

# Validate, migrate, run:
./venv/bin/python -m sonder_runtime preflight --skip-ollama
./venv/bin/python -m sonder_runtime migrate
./venv/bin/python -m sonder_runtime serve      # OpenAI API on 127.0.0.1:11435
# or:
./venv/bin/python -m sonder_runtime repl       # interactive
```

Point any OpenAI-compatible chat UI at `http://127.0.0.1:11435/v1`. State
lives under `$XDG_DATA_HOME/sonder` or `~/.local/share/sonder` on Linux and
`~/Library/Application Support/sonder` on macOS (override with `SONDER_HOME`).

On Windows PowerShell, use the same commands with
`venv\Scripts\python.exe` / `venv\Scripts\pip.exe`. State normally lives at
`%LOCALAPPDATA%\sonder`; minimal service environments fall back through
`%USERPROFILE%`, then `%SystemDrive%\Sonder`.

## Server-private (production)

Use the installer, which creates a dedicated OS user, installs an
immutable versioned release under `/opt/sonder`, writes `0600` secrets
(never printing the key), and installs hardened systemd units:

```bash
sudo packaging/install_sonder.sh
sudo systemctl enable --now sonder
```

Full walkthrough: [install-server-private](../runbooks/install-server-private.md).
Remote access is a TLS reverse proxy in front of the loopback listener —
never a direct public bind ([secure-remote-access](../runbooks/secure-remote-access.md)).

## First-run sanity checks

```bash
curl -s http://127.0.0.1:11435/live      # {"status":"alive"}
curl -s http://127.0.0.1:11435/ready     # ready once Ollama is reachable
curl -s http://127.0.0.1:11435/version
```

If `/ready` is 503 with "required dependency ollama": start Ollama; the
runtime recovers on its own (it probes every 15s), no restart needed.

## Clients

- **OpenAI-compatible UIs** — base URL `http://<host>:11435/v1`, any bearer
  key when `SONDER_API_KEY` is set.
- **REPL** — `python -m sonder_runtime repl`; slash commands like `/stats`,
  `/run`, `/permissions`, `/autopilot`.
- **MCP** — `python -m sonder_runtime mcp` exposes the tool surface to MCP
  clients.
- **Flutter app** — in `app/`; its System page shows version, update
  status, and rollback ([Update Manager](13-update-manager.md)).

## Where to go next

- Turn knobs: [Configuration](03-configuration.md).
- Understand what it does per request: [Architecture](01-architecture.md).
- Make it learn: [Memory & Learning](06-memory-and-learning.md).
- Automate work: [Agent, Autopilot & Fleet](07-agent-autopilot-fleet.md).
