# Multi-node Ollama worker pool

Connect a Sonder coordinator to Ollama instances running on separate hosts.
The coordinator routes inference through least-inflight selection across all
healthy workers, with per-worker capability discovery and bounded failover.

## Architecture

```
  ┌─ Main PC (coordinator) ─────────────────────┐
  │  Sonder Runtime                              │
  │  [ollama] url = local Ollama                 │
  │  [ollama] workers = ["http://node1:11434"]   │
  │  [ollama] trusted_origins = ["10.0.0.0/8"]   │
  └──────────────────────────────────────────────┘
              │ HTTP (private LAN)
              ▼
  ┌─ Node1 (worker) ────────────────────────────┐
  │  Ollama serving models on :11434             │
  │  (no Sonder required on worker-only nodes)   │
  └──────────────────────────────────────────────┘
```

The coordinator's local Ollama (`[ollama].url`) and any `[ollama].workers`
entries form the pool.  Workers are plain Ollama endpoints — they do not
need a Sonder installation unless they also serve as compute nodes.

## Prerequisites

- Dedicated private network between nodes (e.g. 10.77.0.0/24).
- Ollama installed and running on each worker, bound to `0.0.0.0:11434`.
- Models pulled on each worker before the coordinator starts.

## 1. Configure the worker's Ollama

### Linux (native or server)

On each worker node, ensure Ollama listens on all interfaces:

```bash
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

### Windows (recommended for Windows worker nodes)

Run Ollama natively on Windows rather than inside WSL2.  Set machine-level
environment variables:

| Variable | Value |
|---|---|
| `OLLAMA_HOST` | `0.0.0.0:11434` |
| `OLLAMA_MODELS` | Path to your models directory (e.g. `C:\OllamaModels`) |
| `OLLAMA_ORIGINS` | `*` |

For auto-restart on crash or reboot, create a Windows scheduled task that
runs a service host script at startup:

```powershell
# ollama-service.ps1 — run as a scheduled task (SYSTEM or your user)
$OllamaExe = 'C:\Users\<you>\AppData\Local\Programs\Ollama\ollama.exe'
$env:OLLAMA_HOST = '0.0.0.0:11434'
$env:OLLAMA_MODELS = 'C:\OllamaModels'
$env:OLLAMA_ORIGINS = '*'

$restartDelay = 5
while ($true) {
    $proc = Start-Process -FilePath $OllamaExe -ArgumentList 'serve' `
        -PassThru -NoNewWindow
    $proc.WaitForExit()
    Start-Sleep -Seconds $restartDelay
    $restartDelay = [Math]::Min($restartDelay * 2, 60)
    if ($proc.ExitCode -eq 0) { $restartDelay = 5 }
}
```

> **Note:** If running as SYSTEM, `$env:LOCALAPPDATA` resolves to the
> system profile — hardcode the full path to `ollama.exe` instead.

Disable any other auto-start mechanisms (Ollama startup folder shortcut,
older scheduled tasks) to avoid port conflicts.

If the worker also runs Sonder (as a compute node, not just an Ollama
endpoint), use the same scheduled-task pattern for Sonder.  Override
`OLLAMA_HOST=127.0.0.1:11434` in the Sonder service script — if
`OLLAMA_HOST` is set to `0.0.0.0` at the machine level, Sonder picks
it up and rejects it as non-loopback.

### WSL2 — not recommended for workers

WSL2 networking is unreliable for LAN-facing services.  With
`networkingMode=mirrored`, ports bind in the shared namespace but the
Hyper-V firewall blocks LAN inbound by default.  `netsh portproxy`
forwarding causes bind conflicts and TIME_WAIT socket buildup that
produces empty replies.

**Prefer Windows-native Ollama** (above) for worker nodes.  If you must
use WSL2, test LAN reachability thoroughly and expect to troubleshoot
Hyper-V firewall rules.

### Verify from the coordinator

```bash
curl -s http://<worker-ip>:11434/api/version
```

## 2. Configure the coordinator

Edit `sonder.toml` on the coordinator (the machine running `sonder serve`):

```toml
[ollama]
url = "http://127.0.0.1:11434"
allow_remote = true
workers = ["http://10.77.0.2:11434"]
trusted_origins = ["10.77.0.0/24"]
```

| Key | Purpose |
|---|---|
| `allow_remote` | Consent gate — must be `true` to reference non-loopback workers. |
| `workers` | Additional Ollama origins. The coordinator discovers each worker's loaded models and routes by least-inflight. |
| `trusted_origins` | CIDR list of private subnets where HTTP (non-TLS) is accepted. Without this, every remote worker must use HTTPS. |

Alternatively, set via environment:

```bash
SONDER_ALLOW_REMOTE_OLLAMA=1
SONDER_OLLAMA_WORKERS=http://10.77.0.2:11434
SONDER_TRUSTED_ORIGINS=10.77.0.0/24
```

## 3. Tier routing with remote models

Tier environment variables map quality tiers to specific models.  The
coordinator selects the worker that advertises the required model — a
model only needs to be pulled on the machine that serves it.

Example two-node setup (coordinator env):

```bash
# Coordinator's local Ollama has: bonsai:27b-q2, qwen3.8:q3
# Remote worker's Ollama has: ornith-1.5:35b, Qwen3.8 Q6/Q8, nomic-embed-text

SONDER_FAST=bonsai:27b-q2                                   # → local
SONDER_CODE=ornith-1.5:35b                                   # → remote (MoE)
SONDER_REASONING=hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q6_K_XL  # → remote (dense Q6)
SONDER_EMBED_MODEL=nomic-embed-text                          # → remote
```

To keep interactive coding on the local machine while offloading
autonomous/heavyweight work to the remote node, set `SONDER_CODE` to a
local model on the coordinator and configure a runtime policy override
that escalates autonomous requests to the remote model.

The tier router classifies prompts as transformation (→ code tier),
recall (→ general tier), or reasoning (→ reasoning tier) and selects
the configured model for that lane.  The pool router then dispatches to
whichever worker advertises the chosen model.

## 4. Verify the pool

```bash
python -m sonder_runtime status --json | jq '.ollama_pool'
```

Each worker shows its health, inflight count, and discovered model
capabilities.  A worker that fails the health probe enters cooldown
(`worker_cooldown_seconds`, default 30s) and re-enters the pool after
recovery.

## 5. Tuning

| Setting | Default | Notes |
|---|---|---|
| `worker_max_inflight` | 1 | Concurrent requests per worker.  Increase only if the worker has enough VRAM to serve multiple slots. |
| `worker_queue_depth` | 32 | Backpressure queue per worker. |
| `worker_failure_threshold` | 3 | Consecutive failures before cooldown. |
| `worker_cooldown_seconds` | 30 | Seconds a failed worker stays out of rotation. |
| `worker_capability_ttl_seconds` | 300 | How often model lists are re-probed. |

## Security considerations

`trusted_origins` bypasses the TLS requirement for the listed CIDRs.  Use
it **only** on physically isolated networks where eavesdropping between
nodes is not a concern (e.g. a direct Ethernet crossover or an isolated
VLAN).  On shared or routable networks, deploy a TLS reverse proxy in front
of each worker's Ollama (see `secure-remote-access.md`) and omit
`trusted_origins`.
