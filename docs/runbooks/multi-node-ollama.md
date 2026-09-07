# Multi-node Ollama worker pool

Connect a Sonder coordinator to Ollama instances running on separate hosts.
The coordinator routes inference through least-inflight selection across all
healthy workers, with per-worker capability discovery and bounded failover.

## Architecture

```text
Coordinator: Sonder + local Ollama
  [ollama].workers = ["https://node1:11443"]
        │ HTTPS over the private network
        ▼
Worker: TLS proxy :11443 → Ollama 127.0.0.1:11434
  No Sonder installation required on a worker-only host.
```

The coordinator's local Ollama (`[ollama].url`) and any `[ollama].workers`
entries form the pool.  Workers are plain Ollama endpoints — they do not
need a Sonder installation unless they also serve as compute nodes.

`worker_pool_max_workers` counts the primary endpoint plus every additional
worker. Each endpoint must be unique after canonical normalization, so a
repeated worker or an alias of the primary endpoint is rejected rather than
silently removed. The bound keeps one coordinator's roster finite; it does
not guarantee that a host can sustain that many workers or any particular
throughput. The roster is static: Sonder only probes the origins an operator
configured. It does not discover remote Ollama nodes, shard one model or
request across nodes, or claim indefinite scaling.

## Prerequisites

- Dedicated private network between nodes (e.g. 10.77.0.0/24).
- Ollama installed on each worker, bound to `127.0.0.1:11434`, behind a TLS reverse proxy on an explicitly configured port (for example, 11443).
- Worker TLS certificates trusted by the coordinator system trust store, with the worker hostname or IP in the certificate SAN.
- Models pulled on each worker before the coordinator starts.

## 1. Configure the worker's Ollama

### Linux (native or server)

On each worker node, keep Ollama on loopback; the TLS proxy owns the remote listener:

```bash
# /etc/systemd/system/ollama.service.d/override.conf
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
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
| `OLLAMA_HOST` | `127.0.0.1:11434` |
| `OLLAMA_MODELS` | Path to your models directory (e.g. `C:\OllamaModels`) |
| `OLLAMA_ORIGINS` | `*` |

For auto-restart on crash or reboot, create a Windows scheduled task that
runs a service host script at startup:

```powershell
# ollama-service.ps1 — run as a scheduled task (SYSTEM or your user)
$OllamaExe = 'C:\Users\<you>\AppData\Local\Programs\Ollama\ollama.exe'
$env:OLLAMA_HOST = '127.0.0.1:11434'
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
curl --fail https://<worker-host>:11443/api/version
```

## 2. Configure the coordinator

Edit `sonder.toml` on the coordinator (the machine running `sonder serve`):

```toml
[ollama]
url = "http://127.0.0.1:11434"
allow_remote = true
workers = ["https://node1.example:11443"]
```

| Key | Purpose |
|---|---|
| `allow_remote` | Consent gate — must be `true` to reference non-loopback workers. |
| `workers` | Additional explicitly configured Ollama origins. The coordinator probes their model capabilities and routes complete requests by least-inflight. |
| `trusted_origins` | Legacy CIDR metadata, retained for configuration compatibility. It does not authorize remote HTTP or bypass TLS validation. |

Alternatively, set via environment:

```bash
SONDER_ALLOW_REMOTE_OLLAMA=1
SONDER_OLLAMA_WORKERS=https://node1.example:11443
SONDER_OLLAMA_POOL_MAX_WORKERS=16
SONDER_OLLAMA_WORKER_PROBE_PARALLELISM=4
SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE=32
SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE=32
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

The `status` MCP tool and the app/REPL default status render cached aggregate
counts only: eligible/total workers, available slots, the global queue, static
membership, and capability-cache freshness. Unobserved state is `not_refreshed`
or `unknown`. A routine status read performs no Ollama, DNS, hardware, or model
inventory probe and reveals no worker origins or model previews.

An administrator can explicitly call
`ollama_pool_admin_status(refresh=false, cursor="", page_size=32)` for a cached
page. Set `refresh=true` to request exactly one stale-worker batch using the
configured batch size and parallelism. These probe limits cannot be overridden
by tool arguments. Direct local-open use is permitted; authenticated deployments
require an administrator account. HTTP uses the same account role decision,
with the sole owner key accepted in API-key deployments.

HTTP `GET /v1/sonder/ollama-pool?page_size=32` reads cached detail;
`POST /v1/sonder/ollama-pool` accepts `refresh`, `cursor`, and `page_size` only.
The app's **Inspect worker page** and **Refresh worker cache** buttons are explicit
operator actions. Each schema-version-2 page ends on a complete record, has a
65,536 UTF-8-byte ceiling, and includes model counts and at most eight sanitized
128-character model previews. Use `next_cursor` for the next page; it is bound
to one administrator and roster generation. Invalid or stale cursors are
rejected before any probe. Existing count-only version-1 readers remain
compatible. These operations never create or discover membership.

## 5. Tuning

| Setting | Default | Notes |
|---|---|---|
| `worker_pool_max_workers` | 16 | Maximum unique primary-plus-worker roster held by one coordinator (1–256). This is a finite configuration bound, not a throughput guarantee. |
| `worker_max_inflight` | 1 | Concurrent requests per worker.  Increase only if the worker has enough VRAM to serve multiple slots. |
| `worker_queue_depth` | 32 | Bounded backpressure waiters across the whole pool. It is not multiplied by the number of workers. |
| `worker_capability_probe_parallelism` | 4 | Maximum concurrent worker capability probes for this pool (1–8). |
| `worker_capability_probe_batch_size` | 32 | Maximum stale workers selected by one fair refresh pass (1–128). |
| `worker_status_page_size` | 32 | Default bounded cached worker-detail page size (1–128); one page is capped at 65,536 UTF-8 bytes. |
| `worker_failure_threshold` | 3 | Consecutive failures before cooldown. |
| `worker_cooldown_seconds` | 30 | Seconds a failed worker stays out of rotation. |
| `worker_capability_ttl_seconds` | 300 | How often model lists are re-probed. |

## Security considerations

Every non-loopback Ollama endpoint requires explicit remote consent **and
HTTPS**, including isolated private LANs. `trusted_origins` never disables
TLS. Deploy a TLS reverse proxy in front of each worker (see
[secure remote access](secure-remote-access.md)), keep Ollama on loopback, and
restrict the proxy listener to the intended coordinator. Do not disable
certificate verification. The coordinator uses its existing no-proxy,
no-redirect, bounded-response transport and does not replay response-bearing
model requests.
