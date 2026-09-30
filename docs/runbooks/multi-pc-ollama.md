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

For model-specific routing the pool requires fresh positive capability evidence.
If no fresh supporting worker exists, one request may probe one unknown or stale
worker; it does not fan out across the roster. When no eligible worker advertises
the requested model at all, the request forces one bounded capability batch over
eligible workers whose cached inventory lacks it (at most once every 30 seconds
per pool), so a model pulled after the last probe becomes routable without an
operator refresh. This renews inventory only; it never admits a worker that
membership has not activated. Model-less requests and default status start no
such probe. `ollama_pool_admin_status(refresh=true)` is the explicit
administrator operation for one configured bounded stale refresh.

When a chat request prewarms its model, the real request for that model waits
(within its own deadline) for the prewarm to finish before it asks the pool for
admission, so the prewarm cannot hold the worker's only slot and turn the real
request into `timed out waiting for Ollama worker capacity`.
Idempotent control reads may fail over; model POSTs never do. A transport timeout
cannot prove that a remote worker did not receive a request body, so Sonder
surfaces ambiguous failures instead of replaying the POST. Administrative status
reports only closed error categories, never free-form exception text.

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
worker_max_inflight = 1
worker_queue_depth = 32
worker_admission_timeout_ms = 1000
worker_failure_threshold = 3
worker_cooldown_seconds = 30
worker_capability_ttl_seconds = 300
worker_probe_timeout_ms = 2000
```

Every remote worker must use HTTPS, have no credentials embedded in its URL,
have no path/query/fragment in its configured origin, and have the matching
model tag installed. Certificate and hostname verification always apply; there
is no insecure-skip-verify mode. The trust anchors are, in order:
`[ollama].ca_bundle` / `SONDER_OLLAMA_CA_BUNDLE`, else the process-wide bundle in
`SSL_CERT_FILE` or `REQUESTS_CA_BUNDLE` (the one OpenSSL and `requests` already
use), else Python's system trust store. A configured bundle replaces the system
store rather than merging with it: on Windows a stale certificate with the same
subject in the user's CA store otherwise fails verification of a private-CA or
self-signed worker (`self-signed certificate`) even though the bundle trusts it.
Administrative status reports the source as `tls_verification`
(`configured-ca-bundle` or `system-trust-store`). If the consent gate, URL, or
TLS requirements are wrong, startup fails closed rather than silently routing
prompts over an insecure link.

## Membership lifecycle

Configured remote workers start in `probation` and carry no traffic until the
static membership controller has probed them. `serve`, MCP and the REPL start
that controller at launch (first pass immediately, then every
`min(30, worker_capability_ttl_seconds / 2)` seconds), and log each change as
`inference membership: members=N active=N probation=N ...`; a roster with no
active member logs it as a warning. A worker that has not yet had a controller
pass reports `error_category: membership_pending`; a failed probe keeps it in
`probation` with a closed category such as `tls`, `timeout` or `transport` and a
bounded `capability probe failed` warning. The administrator cache refresh
renews capabilities of admitted workers; it does not admit new members.
External membership (`[membership] mode = "external"`) keeps its explicit,
operator-started lifecycle.

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
scheduler above. Locality decisions (error messages and cache
eligibility) also treat any configured remote worker as non-local, not just a
non-loopback primary — a loopback primary with a remote worker in
`SONDER_OLLAMA_WORKERS` is reported and cached as remote.

## Dedicated embedding host

Embeddings do not use the pool: they go to one origin. By default that origin
is the primary. On a machine whose GPU holds one large chat model, every
embedding there competes with that model. With `OLLAMA_MAX_LOADED_MODELS=1`,
Ollama unloads the chat model for it. To move only the embedder to a worker:

```text
SONDER_EMBED_BASE_URL=https://10.77.0.2:8443   # worker behind a TLS proxy
SONDER_ALLOW_REMOTE_OLLAMA=1
SONDER_OLLAMA_CA_BUNDLE=C:\path\to\worker-ca.pem
SONDER_EMBED_MODEL=nomic-embed-text:latest     # must be installed on the worker
SONDER_EMBED_FALLBACK=none                     # or: local (CPU on the primary)
SONDER_EMBED_KEEP_ALIVE=24h                    # keep the embedder resident there
```

- Without `SONDER_EMBED_KEEP_ALIVE`, Ollama unloads an idle embedder after
  five minutes. A worker that is busy with other work can take tens of seconds
  to reload it, which is longer than recall callers wait.

- A worker that is down costs one timed-out call, then a
  `SONDER_EMBED_COOLDOWN_SECONDS` circuit (default 30 s). A 4xx, such as a
  missing model, is a configuration error. It never triggers the fallback.
- `SONDER_EMBED_FALLBACK=local` never sends embeddings to a second remote
  host. It only uses a loopback primary, with `num_gpu: 0`.
- Stored vectors carry model and revision provenance. Using the same model tag
  on both hosts keeps them comparable. A different model needs the usual
  explicit backfill.
- `memory_embedding_backfill`, semantic tier routing, and the learning-health
  revision refresh are loopback-only by design, so they stay off while the
  embedder is remote. For a backfill, unset `SONDER_EMBED_BASE_URL` for that
  run.
- `sonder doctor` reports the `embeddings` check: whether the model is
  installed where embeddings go, and whether the fallback is ready.

## Verify

The normal status surface is a cached summary: eligible/total workers, available
capacity, global queue occupancy, static membership, and cache freshness. It
never refreshes inventory or reveals worker origins. Use the explicit
administrator-only `ollama_pool_admin_status` operation for a bounded cached
page or one configured refresh batch. See the
[multi-node status contract](multi-node-ollama.md#4-verify-the-pool) for the HTTP
route, cursor behavior, and fixed page limits.

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
Prometheus scrape target never learns your worker topology. Only administrator detail exposes configured origins. Each failed attempt's
error text is redacted with the same secret-value and pattern filters as the
structured JSON logs before it is retained for the status surface.

To pull the trace spans for one specific request or run, use the bounded
local observability projection with a correlation filter, e.g.
`GET /v1/observability/trace?correlation_id=<id>`; `category` and `severity`
 filters compose the same way.
 Administrative detail also reports the TLS verification mode and disabled
 non-idempotent failover. Error categories never contain exception text,
 internal topology, or credential-shaped details.

## Internet access

For Internet use, put the worker endpoint behind a VPN or an authenticated TLS
reverse proxy with firewall allow-listing. Never port-forward Ollama's raw
`11434` port or Sonder's loopback service. The coordinator's API authentication
and the worker's TLS boundary are separate controls.
