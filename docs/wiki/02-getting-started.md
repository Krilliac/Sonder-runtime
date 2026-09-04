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
  [facts. runbook](../runbooks/use-facts-model.md)). One generative local
  model is the whole requirement — see
  [Model Requirements & Onboarding](19-model-requirements-and-onboarding.md)
  for what is optional and how to verify what you have.
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

### Extracted local-system bundle

The local-system archive is self-contained and does not change `PATH`. Run
the launcher from its extracted directory:

```powershell
# Windows PowerShell
.\bootstrap-engine.cmd
.\sonder.cmd
```

```bash
# Linux/macOS
./bootstrap-engine.sh
. ./sonder-runtime.sh
"$SONDER_PYTHON" ./sonder_repl.py
```

If a launcher has been installed on `PATH`, invoking `sonder` is equivalent on
Windows (`sonder.cmd`). An extracted POSIX bundle does not ship a bare
`sonder` executable, so retain the explicit command above unless you create a
local wrapper yourself.

## Server-private (production)

Use the installer, which creates a dedicated OS user, installs an
immutable versioned release under `/opt/sonder`, writes `0600` secrets
(never printing the key), and installs hardened systemd units:

```bash
python3 scripts/package_local_system.py --out dist/local-system
sudo packaging/install_sonder.sh --package-source dist/local-system \
  --version-tag "$(git describe --always)"
sudo systemctl enable --now sonder
```

Full walkthrough: [install-server-private](../runbooks/install-server-private.md).
Remote access is a TLS reverse proxy in front of the loopback listener —
never a direct public bind ([secure-remote-access](../runbooks/secure-remote-access.md)).

## WSL2 deployment (Linux inside Windows)

Sonder runs inside WSL2 (Ubuntu 24.04+) the same as native Linux. Key
differences:

- **Resource limits** — set `memory`, `processors`, and `swap` in
  `C:\Users\<you>\.wslconfig` under `[wsl2]`.
- **systemd** — requires `systemd=true` in `/etc/wsl.conf`. Both Ollama
  and Sonder systemd units work normally once enabled.
- **Port access from the Windows host** — WSL2 uses NAT. Forward ports
  with `netsh interface portproxy` if external machines need to reach
  services inside WSL. The WSL2 IP changes on reboot; automate the
  forwarding rule in a startup task.
- **Ollama GPU access** — WSL2 passes through the host GPU automatically
  with an up-to-date Windows driver. Verify with `ollama ps` after loading
  a model.

See [multi-node-ollama](../runbooks/multi-node-ollama.md) for using a WSL2
node as a remote Ollama worker in a multi-machine pool.

## Multi-node Ollama pools

A single coordinator can route inference across Ollama instances on
multiple machines.  Add remote workers in `[ollama].workers` with
`allow_remote = true`.  On private LANs, `trusted_origins` accepts
CIDRs where HTTP (non-TLS) workers are allowed.

Full walkthrough: [multi-node-ollama](../runbooks/multi-node-ollama.md).

## First-run sanity checks

```bash
curl -s http://127.0.0.1:11435/live      # {"status":"alive"}
curl -s http://127.0.0.1:11435/ready     # ready once Ollama is reachable
curl -s http://127.0.0.1:11435/version
```

If `/ready` is 503 with "required dependency ollama": start Ollama; the
runtime recovers on its own (it probes every 15s), no restart needed.

## Clients

- **OpenAI-compatible UIs** — use `http://127.0.0.1:11435/v1` locally or the
  configured `https://` reverse-proxy URL remotely, with the bearer key.
- **REPL** — `python -m sonder_runtime repl`; slash commands like `/stats`,
  `/run`, `/permissions`, `/autopilot`.
- **MCP** — `python -m sonder_runtime mcp` exposes the tool surface to MCP
  clients.
- **Flutter app** — in `app/`; its System page shows version, update
  status, and rollback ([Update Manager](13-update-manager.md)). API and host
  launcher bearer tokens use the platform credential store rather than app
  preferences. Use **Forget local session** to remove the account token from
  this device; copied tokens remain valid until server expiry or revocation.

## Source checkout updates from the REPL

The REPL banner reports the loaded checkout and the newest cached
`origin/main` revision without contacting the network. Type `/updatecheck` to
refresh that ref and see the installed/newest commits and timestamps.
`/update` is a guarded source fast-forward, not a release-manager install: it
only runs for a clean checkout on `main` using the canonical Sonder remote and
refuses local commits, feature branches, or local edits. Restart the REPL
after it succeeds. See [Update Manager](13-update-manager.md) for signed
release bundles and rollback.

When local work needs to be preserved before that guarded update, `/stash`
shows source-recovery readiness. `/stash save` saves tracked source changes;
`/stash save-untracked` also saves generated/untracked files; `/stash pop`
restores the most recent recovery stash only when the canonical `main`
checkout is clean. These commands use no user-selected repository, remote,
revision, or stash selector. Saved workflows are per-user state by default
(`%LOCALAPPDATA%\sonder\workflows.json` on Windows), not installation files.
On first use, an older checkout-root `workflows.json` is copied into that state
home and left in place for review.

## Where to go next

- Turn knobs: [Configuration](03-configuration.md).
- Understand what it does per request: [Architecture](01-architecture.md).
- Make it learn: [Memory & Learning](06-memory-and-learning.md).
- Automate work: [Agent, Autopilot & Fleet](07-agent-autopilot-fleet.md).
