# Multi-node Ollama worker pool

Connect a Sonder coordinator to Ollama instances running on separate hosts.
The coordinator routes inference through least-inflight selection across all
healthy workers, with per-worker capability discovery and bounded failover.

This runbook owns the **worker host setup** (Ollama on Linux or Windows, tier
routing, optional signed external membership). The rest has one home each:

- Pool topology, circuit breaker, coordinator pool settings, membership
  lifecycle and verification: [multi-pc-ollama.md](multi-pc-ollama.md).
- The TLS reverse proxy (Caddy) in front of each worker, and the coordinator
  URL, consent and CA-trust rules: [remote-ollama-caddy.md](remote-ollama-caddy.md)
  (see its "Coordinator settings" section).
- Every `[ollama]` key, environment variable and bound:
  [wiki 03 configuration](../wiki/03-configuration.md).

## Architecture

```text
Coordinator: Sonder + local Ollama
  [ollama].workers = ["https://<worker-host>:11443"]
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
throughput. Static configuration is the default. The optional authenticated
membership mode below can admit only origins an operator already authorized.
Sonder does not discover remote Ollama nodes, shard one model or
request across nodes, or claim indefinite scaling.

## Prerequisites

- Dedicated private network between nodes (e.g. 10.0.0.0/24).
- Ollama installed on each worker, bound to `127.0.0.1:11434`, behind a TLS reverse proxy on an explicitly configured port (for example, 11443; the other runbooks use 443, and either works as long as the worker URL names the same port).
- Worker TLS certificates the coordinator trusts, with the worker hostname or IP in the certificate SAN. The trust-anchor order (CA bundle setting, then process-wide bundle, then system store) is in [remote-ollama-caddy.md](remote-ollama-caddy.md#coordinator-settings).
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

For auto-restart on crash or reboot, create a Windows scheduled task that
runs a service host script at startup:

```powershell
# ollama-service.ps1 — run as a scheduled task (SYSTEM or your user)
$OllamaExe = '<full-path-to>\ollama.exe'   # default install: %LOCALAPPDATA%\Programs\Ollama\ollama.exe, written out in full
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MODELS = 'C:\OllamaModels'

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
> Sonder sends no browser `Origin` header, so leave `OLLAMA_ORIGINS` at its
> default; do not set it to `*`.

Disable any other auto-start mechanisms (Ollama startup folder shortcut,
older scheduled tasks) to avoid port conflicts.

If the worker also runs Sonder (as a compute node, not just an Ollama
endpoint), use the same scheduled-task pattern for Sonder.  Set
`OLLAMA_HOST=127.0.0.1:11434` in the Sonder service script. Sonder reads
`OLLAMA_HOST` too and requires an explicit port: a value with no port (for
example a bare `127.0.0.1`) fails with `OLLAMA_HOST must include an explicit
port` (`sonder_runtime/domain/ollama_policy.py:156-157`). A bare `0.0.0.0` (the common
Ollama setting for "listen on all interfaces") fails the same explicit-port
check. A value with a port, such as `0.0.0.0:11434` or
`http://0.0.0.0:11434`, is accepted: Sonder rewrites it to `127.0.0.1` and logs
a warning (`sonder_runtime/domain/ollama_policy.py:25-29`, `:92-97`, `:156-157`). Ollama
itself would still listen on every interface, so keep the machine-level value
on loopback.

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
curl --cacert <ca-file-given-to-sonder> https://<worker-host>:11443/api/version
```

Use the same CA file you set in `[ollama].ca_bundle` (omit `--cacert` for a
publicly trusted certificate) and `curl.exe` in PowerShell. Expected: a JSON
body such as `{"version":"..."}`. A TLS error means the coordinator does not
trust the proxy certificate. The full check list is
[remote-ollama-caddy.md](remote-ollama-caddy.md#verify); fixes are under
[Troubleshooting](remote-ollama-caddy.md#troubleshooting).

## 2. Configure the coordinator

Coordinator pool settings (`[ollama].allow_remote`, `workers`, the tuning keys,
the consent gate and the certificate-trust order) live in
[multi-pc-ollama.md](multi-pc-ollama.md#configure-the-coordinator-pc) and
[remote-ollama-caddy.md](remote-ollama-caddy.md#coordinator-settings). The full
key, environment-variable and bounds table is
[wiki 03](../wiki/03-configuration.md). Only these worker origins are needed
here:

```toml
[ollama]
workers = ["https://worker1.example:11443"]
```

Membership: `serve`, `repl` and `mcp` start the static membership controller at
launch (`sonder_runtime/bootstrap/app.py:2330-2348`, called from `sonder_runtime/__main__.py:1292`, `:1419`,
`:1470`, `:1524`), so configured workers leave `probation` without any extra
call. A process that builds the application directly (an embedding owner)
starts nothing until it calls `application.inference_membership.refresh(...)` or
`start()`. External mode (`[membership] mode = "external"`) is never started
automatically. There is no membership-enrollment UI or CLI.

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

## 5. Tuning and security

Tuning keys, defaults and bounds are in
[wiki 03](../wiki/03-configuration.md) (one home). Security rules for remote
workers (HTTPS only, no insecure-skip-verify, proxy source-IP allowlist) are in
[remote-ollama-caddy.md](remote-ollama-caddy.md) and
[secure remote access](secure-remote-access.md). `trusted_origins` is legacy
metadata and never disables TLS.

## Optional externally authenticated membership

External mode requires a fixed registry serving `GET /v1/membership`, a private
CA, a PEM Ed25519 public signing key, and client certificate/key files supplied
through the secrets environment. The registry and every worker must require
that client certificate. This is a configured membership authority, not node
discovery, an ownership service, or automatic takeover.

```toml
[ollama]
url = "http://127.0.0.1:11434"
allow_remote = true
worker_pool_max_workers = 16

[membership]
mode = "external"
cluster_id = "private-inference"
issuer_id = "configured-registry"
protocol_version = 1
source_origin = "https://registry.example:443"
source_tls_server_name = "registry.example"
source_allowed_cidrs = ["10.0.0.0/24"]
trust_anchor_file = "/private/config/inference-ca.pem"
signature_public_key_file = "/private/config/membership-signer.pem"
refresh_interval_seconds = 30
snapshot_max_advertisements = 4096
snapshot_max_bytes = 1048576
local_fallback = false

[[membership.member_policies]]
member_id = "worker1"
origin = "https://worker1.example:11443"
tls_server_name = "worker1.example"
allowed_cidrs = ["10.0.0.0/24"]
```

Use absolute file paths suitable for the coordinator OS. Set
`SONDER_MEMBERSHIP_CLIENT_CERT_FILE` and `SONDER_MEMBERSHIP_CLIENT_KEY_FILE` in
the protected secrets environment file or process environment; credential
settings in TOML are rejected. Membership configuration and public status
redact origins, certificate identities, CIDRs, trust paths, and credential
paths. The exact typed application, pool, controller, configured policies,
clock, source, and state store are checked at compatibility bindings. External
mode must use that typed composition; a bare remote `python server.py` root
fails closed without it.

Authority and client credential paths must be absolute local paths to private,
single-link regular files beneath private directories. Relative paths, UNC or
mapped network drives on Windows, symlinks/junctions, special files, foreign
owners and public file permissions are rejected. The runtime trusts the local
owner to provision the four files before application construction. It captures
bounded bytes using directory anchors and file identity/permission checks;
the signing key and private TLS context use only those captured bytes for that
application lifetime. Subsequent file replacement or edits cannot rotate live
authority: restart the application to adopt a deliberate credential/CA/signing
rotation. TLS client-chain loading briefly creates private temporary copies
and removes them before connecting. This does not defend against a compromised
local owner or administrator controlling the running process.

The default embedding provider is unavailable in external membership mode,
including with `local_fallback = true` and with valid admitted members. It
fails before using the generic Ollama transport; typed custom embedding
providers remain explicitly supplied integrations. Static-mode default
embeddings retain their existing behavior.
This restriction also covers the shared legacy adapter used by answer capture,
fact memory, lesson distillation, examples, and embedding backfill, including
MCP, HTTP, REPL and bound-direct calls. Those legacy operations retain their
existing soft-failure behavior and can store memory without vectors. The
process-default adapter stays disabled while any composed external source is
still owned, including a closed, expired or revoked source. Constructing an
unrelated static graph cannot clear that restriction; a static-only process
with no external source retains default embeddings. Each complete default
embedding or revision/provenance operation holds an active-operation lease
covering checks, metadata transport, text conversion, JSON serialization and
embedding transport. Registration first refuses new operations, then waits up
to five seconds for existing operations to finish before activating ownership.
If they do not finish, source construction fails without publishing ownership
or leaving a pending restriction. Same-thread registration from inside an
active operation is rejected to avoid a callback deadlock.
Static operations can run concurrently; the short policy lock protects only
counts and ownership, and is released during network, cache and accelerator
work. Nested provenance calls inherit the existing operation so pending
registration cannot prevent that operation from completing. Existing transport
timeouts still apply; registration's five-second bound does not cancel an
earlier static operation.
The ownership fence also survives staged live reloads of the embedding and
endpoint-policy modules.

Standalone preflight and doctor Ollama checks are explicitly deferred in
external mode. They perform no registry or worker I/O and expose no endpoint
details; readiness requires the typed pool's explicit membership/capability
refresh. Static-mode diagnostic behavior is unchanged.

Each member ID has exactly one canonical HTTPS origin with an explicit port,
exact hostname/IP SAN, and 1–32 explicit CIDRs. Wildcards, suffix matching,
CN fallback, duplicate IDs/origins, remote HTTP, and `/0` CIDRs are rejected.
The private CA replaces system trust for both registry retrieval and worker
capability/inference requests. Each connection resolves once, checks every
answer against its configured CIDRs, then connects to a validated numeric
address with the configured SNI and exact SAN. Proxies and redirects are never
used. A signed advertisement cannot change any transport or credential policy.
Only a body-free `GET /api/version` 404 retains the older-worker compatibility
fallback to `/api/tags`. Other response-bearing failures remain closed and
cannot trigger inference replay.
This exception requires identity encoding, exactly one `Content-Length: 0`,
no transfer encoding, and EOF on the bounded raw reader. Unknown, nonempty,
truncated, oversized, transfer-encoded or extra response bodies are rejected.

The signed response is compact ASCII JSON with sorted keys and no whitespace
or trailing newline: `{"payload":...,"signature":"..."}`. The payload has
exactly `cluster_id`, `issuer_id`, `generation`, `protocol_version`, `issued_at`,
`expires_at`, and `workers`. The signature is base64 Ed25519 over the same
canonical encoding of the payload. Times are timezone-aware ISO 8601 strings.
Each worker has exactly `worker_id`, `origin`, `member_generation`,
`lifecycle_state`, `models`, and `advertised_capacity`; identities and origins
must match local policy. Supported lifecycle states are `probation`, `active`,
`draining`, `expired`, `unhealthy`, and `revoked`. An advertised active worker still needs
fresh local capability evidence. The replay digest covers the entire verified
canonical envelope, including its signature.

Configuration and snapshots admit at most 4,096 policies/advertisements and a
1 MiB response. The pool still holds at most `worker_pool_max_workers` total
states (default 16, maximum 256), including reserved configured loopback and
draining slots. Excess membership is omitted with bounded counts; omitted
workers are not probed or sent inference. Normal probe batch/parallelism and
status page limits still apply. Source refresh is 1–86,400 seconds; one source
fetch is bounded by the controller's at-most-30-second deadline. A timed-out
resolver retains its single owned task until it finishes, with no queued
replacement. Worker requests and responses are each capped at 1 MiB, with a
maximum 300-second transport deadline. These limits do not promise throughput.

`local_fallback` is an explicit external-mode choice. `false` disables the
separate configured loopback pool inference lane. `true` permits only the exact
configured loopback primary/workers, including during source outage; it cannot
authorize a remote worker. Loopback endpoints are never externally admitted.
On registry failure, only the last accepted, unexpired remote roster with
fresh capability evidence remains eligible. Expiry, revocation, or removal
stops new remote admissions; existing in-flight requests retain their original
endpoint while draining. No default status or inference request refreshes the
external source.

Before applying a newer roster, the controller persists exactly
`{cluster, issuer, generation, digest}` as canonical JSON at
`<runtime-state>/inference-membership/high-water.json`. The dedicated directory
is created privately on first successful advancement. Reads and atomic
replacement hold a nonblocking OS lock and validate the private directory/file
identities, permissions, and absence of links. Staged files are fsynced; Windows
publication uses write-through replacement, and POSIX publication fsyncs the
directory. State/source I/O occurs outside the pool condition.

Lower generations, equal-generation digest conflicts, expired envelopes,
changed cluster/issuer, malformed/partial state, and an initialized directory
missing its record fail closed. Persistence failure leaves the accepted live
roster unchanged, still subject to expiry. Equal generation and digest are
idempotent only before the signed expiry. There is no automatic state reset,
deletion, rotation, or migration. Protect and back up the entire state directory;
do not precreate its private child or restore an older copy to reset admission.
A privileged owner rolling back or deleting the entire directory across a
process restart cannot be detected without an independent monotonic anchor;
this local adapter does not supply one. Filesystem durability also depends on
the host filesystem and hardware honoring synchronization.

Validation uses deterministic fixtures and synthetic loopback TLS servers.
It does not establish live-cluster compatibility or deployed-node readiness.

The [disposable membership acceptance procedure](inference-membership-acceptance.md)
separates synthetic 16/64/256-member evidence from live private-cluster checks,
including revocation, expiry, saturation, DNS policy, cleanup and evidence custody.
Model-specific static-pool requests probe at most one fair concurrent wave of
unknown/stale workers (the smaller of the probe parallelism and batch size, so
the wave itself takes no longer than one probe timeout); concurrent callers
join another request's wave through the same global admission queue, within
their admission timeout, and start none of their own. A caller that waited
behind an administrator or controller batch starts its wave only if the model
is still unserved. Missing or failed capability evidence returns
capability-unavailable
without selecting an unprobed worker. Inventory hints alone do not authorize
model-specific dispatch. Explicit administrator refreshes retain bounded batch
discovery; model-less requests and default status do not start probes. Remote
membership activation and renewal remain owned by the explicit controller.
