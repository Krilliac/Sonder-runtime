# Multi-PC Ollama inference

Sonder can distribute independent inference requests across multiple Ollama
hosts. This is request-level pooling: every host has its own model files and
memory, and Sonder does not shard one model across machines.

## Topology

```text
PC 1: Sonder coordinator ── HTTPS ──> PC 2: TLS proxy ── loopback ──> Ollama
       local Ollama is worker 1       PC 3 may be added the same way
```

The coordinator uses least-inflight scheduling with a latency tie-break:
when several workers are equally idle, the host with the lowest exponentially
weighted average of past request latencies is tried first, and a worker whose
latency has never been measured sorts ahead of all measured ones so it gets
measured. A worker is circuit-broken after three transport failures, and a
request fails over only before a worker returns a response. A model error or
a completed response is never replayed on another PC.

Circuit recovery is half-open: after the cooldown expires the worker admits a
single trial request at a time. A successful trial closes the circuit and
restores normal scheduling; a failed trial re-trips it immediately and the
cooldown doubles on each consecutive trip, up to eight times the base cooldown
(30 s base, 240 s cap). This keeps a permanently-down PC from absorbing a
probe every 30 seconds while still recovering a rebooted one within a minute
or two.

The pool also carries an experimental model-affinity seam: when a worker's
advertised model inventory has been recorded, requests naming a model that
worker lacks are ordered toward workers that have it. Inventory only reorders
scheduling — it never excludes a worker, because a recorded list may be stale
and Ollama can pull a model on demand. Running the `status` tool refreshes
each worker's inventory (best-effort, per-worker `/api/tags` probes) and a
 worker that cannot answer keeps its previous record. Idempotent control-plane
 reads may fail over; model POSTs never do. A transport timeout cannot prove
 that a remote worker did not receive a request body, so replaying that POST
 could duplicate inference. Sonder surfaces the ambiguous failure instead.
Transport errors retained in status are reduced to their exception class.

## Prepare each worker PC

Install Ollama and pull the exact model aliases used by the coordinator:

```powershell
ollama pull sonder:latest
ollama pull qwen3-coder:30b-a3b-q4_K_M
```

Keep Ollama bound to loopback and put a TLS reverse proxy in front of it. The
proxy certificate must be trusted by the coordinator; use a private CA for a
direct Ethernet/Wi-Fi network or place both PCs behind a VPN. Do not expose
plain Ollama HTTP directly to the LAN or Internet.

For example, with Caddy on the worker PC:

```text
ollama.example.internal {
    reverse_proxy 127.0.0.1:11434
}
```

Use a hostname that resolves across the private link and verify from the
coordinator that `https://ollama.example.internal/api/version` is reachable.

## Configure the coordinator PC

Leave the coordinator's own Ollama as the primary endpoint and add remote
workers in its secrets environment or process environment:

```powershell
$env:OLLAMA_HOST = "http://127.0.0.1:11434"
$env:SONDER_ALLOW_REMOTE_OLLAMA = "1"
$env:SONDER_OLLAMA_WORKERS = "https://ollama-pc2.example.internal:443;https://ollama-pc3.example.internal:443"
python -m sonder_runtime preflight
python -m sonder_runtime serve
```

The equivalent TOML configuration is:

```toml
[ollama]
url = "http://127.0.0.1:11434"
allow_remote = true
workers = [
  "https://ollama-pc2.example.internal:443",
  "https://ollama-pc3.example.internal:443",
]
```

Every remote worker must use HTTPS, have no credentials embedded in its URL,
have no path/query/fragment in its configured origin, and have the matching
model tag installed. Certificate and hostname verification use Python's system
trust store; there is no insecure-skip-verify mode. If the consent gate, URL,
or TLS requirements are wrong, startup fails closed rather than silently
routing prompts over an insecure link.

## What never leaves the primary endpoint

A few requests carry a stricter promise than ordinary pooled inference and are
pinned to the primary (`OLLAMA_HOST`) endpoint regardless of pool
configuration — they are refused outright if the primary itself is not
loopback, and the pool is never consulted for them even when it is enabled:

- Vision analysis (`vision_analyze`) — image bytes never leave this machine.
- Fanout synthesis (`model_fanout_synthesize`) — the combined receipt stays on
  the host.

Every other pool-eligible request (ordinary chat/generate tiers) is free to
land on any configured worker, local or remote, per the least-inflight
scheduler above. Locality displays (`status`, error messages, cache
eligibility) also treat any configured remote worker as non-local, not just a
non-loopback primary — a loopback primary with a remote worker in
`SONDER_OLLAMA_WORKERS` is reported and cached as remote.

## Verify

 Use the normal status surface. It reports the configured worker count, how
many workers are remote, and per-worker health: in-flight requests, consecutive
failures, circuit trips, whether the worker is in its half-open probing state,
and the smoothed request latency in milliseconds. A worker that goes down is
temporarily removed from selection and returns automatically through a
successful half-open trial after its cooldown.

Run `python -m sonder_runtime doctor` (or `sonder doctor`) to check worker
health without sending inference traffic:

- **`ollama_workers`** probes every configured worker's `/api/tags`
  independently and reports which ones answered. `ok` means every worker
  responded; `warn` names the unreachable ones while the rest still serve
  requests; `fail` means none of the configured workers answered. A
  single-endpoint deployment (no `workers` configured) reports `skipped`
  here — that is expected, not an error.
- **`ollama_residency`** reads `/api/ps` on the primary endpoint and flags
  any resident model whose `keep_alive` deadline (`expires_at`) has already
  passed. Ollama should have unloaded that model; still seeing it usually
  means eviction stalled (e.g. after a killed or hung generation) and the
  model is pinned in VRAM. This check only observes — it never unloads a
  model itself.

Pass `--skip-ollama` to omit `ollama`, `ollama_workers`, and
`ollama_residency` when you only want the non-network checks, and `--json`
for a machine-readable report to gate scripts or CI on.

When Prometheus metrics are enabled (`prometheus_client` installed and
`SONDER_METRICS=1`), each worker also gets its own bounded slot in the
metrics endpoint:

- `sonder_ollama_worker_requests_total{worker,result}` -- attempts per worker
  by `ok`/`error`.
- `sonder_ollama_worker_duration_seconds{worker}` -- per-worker request
  latency histogram.
- `sonder_ollama_worker_circuit_state_total{worker,state}` -- circuit breaker
  `open`/`closed` transitions per worker.

The `worker` label is a bounded ordinal ("w0", "w1", ...) assigned in
configuration order, capped at 16 distinct slots with any remainder
collapsed into `overflow` -- it never carries the worker's hostname, so a
Prometheus scrape target never learns your worker topology. Use the status
surface (not metrics) to map a slot back to an origin. Each failed attempt's
error text is redacted with the same secret-value and pattern filters as the
structured JSON logs before it is retained for the status surface.

To pull the trace spans for one specific request or run, use the bounded
local observability projection with a correlation filter, e.g.
`GET /v1/observability/trace?correlation_id=<id>`; `category` and `severity`
 filters compose the same way.
 The status surface also reports the TLS verification mode and that
 non-idempotent failover is disabled. Error status retains only the exception
 class; free-form transport details are suppressed because they may contain
 internal topology or credential-shaped text.

## Internet access

For Internet use, put the worker endpoint behind a VPN or an authenticated TLS
reverse proxy with firewall allow-listing. Never port-forward Ollama's raw
`11434` port or Sonder's loopback service. The coordinator's API authentication
and the worker's TLS boundary are separate controls.
