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
measured. A worker is circuit-broken after three consecutive failures (transport errors and 502, 503 or 504), and a
request fails over only before a worker returns a response. A model error or
a completed response is never replayed on another PC.

Failover-eligible outcomes are transport errors that happen before any
response and HTTP 502, 503 and 504, for requests the caller declared
idempotent (`sonder_runtime/adapters/inference/ollama_pool.py:61`, `:1121-1135`,
`:2236-2254`). Ordinary chat requests are declared idempotent (`server.py:1911`,
then `_post_model('/api/chat', ...)` at `server.py:5277` and
`OLLAMA_POOL.request` at `server.py:5554`), so a Caddy 502 on a normal chat
request is sent to another worker and counts toward the circuit.
`/api/generate` calls (model load, unload and prewarm) are sent with `_post`
and its default `idempotent=False` (`server.py:5512-5519`), so they are not
declared idempotent and never fail over; a 502 on one surfaces as an error.
Calls that must not be replayed (`single_send` local calls) and calls pinned to
the primary never fail over.
Any other status, including 500, 501, 404 and 403, is not failed over (only
502, 503 and 504 are) and surfaces as the model error. Agent, autopilot and
fleet callers wait up to 30 s by default for pool capacity
(`SONDER_POOL_ADMISSION_TIMEOUT_SECONDS`, maximum 60), but only when every pool
origin is loopback, `localhost` or a literal private or link-local IP address
(`sonder_runtime/adapters/inference/ollama_pool.py:157-168`, `:771`, `:2035`).
A worker addressed by a DNS name, such as the Caddy names used below, is not
local for this test, so those callers get `worker_admission_timeout_ms`
(1 s by default) instead. Defaults, bounds and this rule are in
[wiki 03](../wiki/03-configuration.md).

Circuit recovery is half-open: after the cooldown expires the worker admits a
single trial request at a time. A successful trial closes the circuit and
restores normal scheduling; a failed trial re-trips it immediately and the
cooldown doubles on each consecutive trip, up to eight times the base cooldown
(30 s base, 240 s cap). This keeps a permanently-down PC from absorbing a
probe every 30 seconds while still recovering a rebooted one within a minute
or two.

For model-specific routing the pool requires fresh positive capability evidence.
If no fresh supporting worker exists, one request may probe one concurrent wave
of unknown or stale static workers, at most the smaller of the configured probe
parallelism (4 by default) and batch size, so a model served by any of them is
found on that request. The wave takes no longer than one probe timeout and does
not fan out across a larger roster; workers beyond one wave are left to later
requests. A request that arrives while another request's wave is in flight
waits for it, within its admission timeout, instead of starting another; one
that waited behind an operator refresh probes its own wave only if the model is
still unserved. When no eligible
worker advertises the requested model at all, the request forces one bounded
capability batch over
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

Every admitted request holds its worker slot through a lease that names the
caller, model and phase (`dispatching`, `gpu_admission`, `transport`). Every
exit path closes the lease, including a `BaseException` such as `KeyboardInterrupt`.
A lease whose thread died without closing it is reclaimed the next time the
pool summary, the status page or an admission timeout looks at the pool. A
lease held longer than two minutes is logged once by the same checks. An admission timeout names
the oldest lease holding the saturated worker. The summary reports `leases`
(held, oldest age and phase, reclaimed count). A request that has to queue in
GPU admission returns its pool slot while it waits, so work that needs no GPU
(a cloud call through the same primary, say) is not starved behind it. Once
admitted it takes a slot back ahead of fresh requests. That wait is capped at
`SONDER_POOL_ADMISSION_TIMEOUT_SECONDS` (or the request deadline, if sooner)
and then refuses with backpressure, releasing the GPU slot. A request that
comes back to a worker that tripped its circuit in the meantime is refused
without sending. Only the request that holds a worker's half-open trial can
end it (or a success, since the worker answered), so another request finishing
on the same worker does not reopen it early. Pool refusals reach chat as
`ERROR contacting Sonder's model pool: ...` with a retry hint, not as "is the
Ollama server running?". Worker ranking keeps two latency averages that are
never compared with each other: control-plane round-trips (capability probes
and model-less reads) and model-pinned inference time, which excludes time
spent in GPU admission. Inference requests rank by inference time once every
candidate worker has one.

A capability probe reads `/api/version`, `/api/tags` and, best effort,
`/api/ps` (which models are loaded now and at what context window). Per-model
metadata from `POST /api/show` (trained context length, tool support, MoE
active parameters) is never fetched inside the probe: the probe reports what
is cached and starts at most one background fill per worker for up to eight
uncached models, so a slow or hanging `/api/show` cannot fail the probe and a
large inventory fills in over successive probes. The cache is keyed by worker,
model and digest (a re-pulled model is read again), and a failed `/api/show`
is retried with exponential backoff (30 s doubling to 10 min). For a loaded
model the `/api/ps` window wins over the trained maximum. Stock Ollama reports
no host VRAM, RAM or KV-cache type, so the probe never invents them. A
model-pinned request then ranks candidate workers in this order: workers whose
inventory lists the model, then workers that have it loaded, then workers whose
known window covers the request's `options.num_ctx`, then in-flight work times
latency. If a TLS proxy filters paths, let `GET /api/ps` and `POST /api/show`
through; without them routing still works, only less informed.

Idempotent control reads may fail over; model POSTs never do. A transport timeout
cannot prove that a remote worker did not receive a request body, so Sonder
surfaces ambiguous failures instead of replaying the POST. Administrative status
reports only closed error categories, never free-form exception text.

## Prepare each worker PC

Install Ollama, then give the worker every model name the coordinator will
request. `sonder:latest` is **not** downloadable: it is a local alias that
`setup_alias.py` builds with `ollama create` (`setup_alias.py:174-193`), and
`ollama pull sonder:latest` fails because no such model is in the Ollama
library. The Caddy reference config also denies `/api/create` and `/api/copy`
through the proxy (`Caddyfile.ollama-worker`), so the coordinator cannot create
it remotely. Create it on the worker itself, against the worker's own loopback
Ollama:

```powershell
# 1. Pull a real base model (the coordinator's chosen base tag, for example):
ollama pull qwen2.5-coder:7b          # use the tag your coordinator aliases
# 2a. Quick alias with the same weights (no baked system prompt):
ollama cp qwen2.5-coder:7b sonder:latest
# 2b. Or an alias with the same settings as Sonder's own: save the two lines
#     below (plus an optional SYSTEM block) as Modelfile, then create it.
#       FROM qwen2.5-coder:7b
#       PARAMETER temperature 0.2
ollama create sonder:latest -f Modelfile
ollama list                           # must show sonder:latest
```

Sonder's own alias is `FROM <base>`, `PARAMETER temperature 0.2` and a
`SYSTEM` block rendered from `prompts/ollama_alias_system.md`
(`setup_alias.py:51-58`). Pick one of 2a or 2b: the coordinator sends its own
messages, so the alias only has to exist and point at the model you want; use
2b or the checkout route when you want the worker's baked system prompt and
temperature to match the coordinator exactly. If the worker has a Sonder
checkout, `python setup_alias.py --model <base-tag>` does the pull and the
create in one step (`--no-embedding` skips the embedding model, `--offline`
uses only models already present; `setup_alias.py:198-235`). The base tag, when
you do not pass `--model`, comes from `SONDER_BASE_MODEL` or the RAM-based
choice in `bootstrap_engine.choose_model` (`bootstrap_engine.py:117-126`): `qwen2.5-coder:7b` from 8 GB of RAM, `3b` from 4 GB, else `1.5b`
(`sonder_runtime/adapters/data/model_lists.json:186-190`).
Real library tags, such as `qwen3-coder:30b-a3b-q4_K_M`, are pulled normally
with `ollama pull`. Any other model name the coordinator requests (check the
tier settings in [wiki 08](../wiki/08-model-tiers-and-gateway.md)) must exist
on the worker under exactly that name.

Then, on each worker PC:

1. Prepare Ollama itself (loopback bind, auto-restart, Windows versus WSL2) by
   following [multi-node-ollama.md](multi-node-ollama.md#1-configure-the-workers-ollama).
2. Put a TLS reverse proxy in front of it and make its certificate trusted by
   the coordinator. Caddy install, Caddyfile, firewall and the private-CA
   options are in [remote-ollama-caddy.md](remote-ollama-caddy.md). Do not expose
   plain Ollama HTTP directly to the LAN or Internet.
3. From the coordinator, run the checks in
   [remote-ollama-caddy.md](remote-ollama-caddy.md#verify). They use
   `curl --cacert <ca-file-given-to-sonder> https://<worker-host>:<proxy-port>/api/version`
   (`curl.exe` in PowerShell) and expect a JSON `{"version":...}` body. The
   proxy port (443 here, 11443 in
   [multi-node-ollama.md](multi-node-ollama.md)) is your choice; use the same
   one in the worker URL. A TLS error is a trust problem; see
   [Troubleshooting](remote-ollama-caddy.md#troubleshooting).

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

The URL rules (HTTPS, explicit port, no credentials or path), the consent gate
and the certificate-trust order (including that a configured CA bundle replaces
the system store) have one home:
[remote-ollama-caddy.md](remote-ollama-caddy.md#coordinator-settings). The full
`[ollama]` key, environment variable and bounds table is in
[wiki 03](../wiki/03-configuration.md). Each worker also needs the matching
model tag installed. If the consent gate, URL, or TLS requirements are wrong,
startup fails closed rather than silently routing prompts over an insecure
link. Administrative status reports the trust source as `tls_verification`
(`configured-ca-bundle` or `system-trust-store`).

Static mode sends no credentials to workers (request headers are
`Content-Type` only, `server.py:5531-5534`; metadata reads go through
`ollama_pool.admitted_get`, `server.py:5622`); client
certificates are used only in external-membership mode
(`SONDER_MEMBERSHIP_CLIENT_CERT_FILE` / `_KEY_FILE`, see
[multi-node-ollama.md](multi-node-ollama.md#optional-externally-authenticated-membership)).
Restrict who can reach the worker with the proxy's source-IP allowlist, not a
header.

## Membership lifecycle

Configured remote workers start in `probation` and carry no traffic until the
static membership controller has probed them. `serve`, MCP and the REPL start
that controller at launch (first pass immediately, then every
`max(1, min(30, worker_capability_ttl_seconds / 2))` seconds,
`sonder_runtime/bootstrap/app.py:602`), and log each change as
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
configuration:

- Vision analysis (`vision_analyze`) — image bytes never leave this machine.
  It is refused outright with `vision analysis requires a loopback Ollama
  endpoint` if the primary is not loopback
  (`sonder_runtime/adapters/inference/ollama_vision.py:89`).
- Fanout synthesis (`model_fanout_synthesize`) is also local-only: it is
  refused unless the primary is loopback, and it is sent to the primary only
  (`server.py:24424-24428`, `:4905-4911`, `:5545-5549`).

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
SONDER_EMBED_BASE_URL=https://<embed-host>:443   # worker behind a TLS proxy
SONDER_ALLOW_REMOTE_OLLAMA=1
SONDER_OLLAMA_CA_BUNDLE=C:\path\to\worker-ca.pem
SONDER_EMBED_MODEL=nomic-embed-text:latest     # must be installed on the worker
SONDER_EMBED_FALLBACK=none                     # or: local (CPU on the primary)
SONDER_EMBED_KEEP_ALIVE=24h                    # keep the embedder resident there
SONDER_EMBED_NUM_CTX=4096                      # bound the embedder's runner context
```

The knob definitions, defaults and bounds are in
[wiki 03](../wiki/03-configuration.md); the notes below explain why each is
worth setting.

- Without `SONDER_EMBED_KEEP_ALIVE`, Ollama unloads an idle embedder after
  five minutes. A worker that is busy with other work can take tens of seconds
  to reload it, which is longer than recall callers wait.
- Without `SONDER_EMBED_NUM_CTX`, Ollama sizes the embedder's runner to the
  worker's default context. On a worker with a large GPU budget that default
  is 32k: a 4 GB embedder became a 10.8 GB runner (measured 2026-09-30,
  `qwen3-embedding:4b-q8_0` on a 46 GB node) and could not stay resident next
  to a 22 GB reasoning model. Embeddings are capped at `SONDER_EMBED_MAX_CHARS`
  characters, so `4096` is a safe bound; Ollama clamps it to a smaller model
  maximum (nomic: 2048). Values below 256 are ignored.

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
Administrative detail also reports the TLS verification mode and disabled
non-idempotent failover. Error categories never contain exception text,
internal topology, or credential-shaped details.

To pull the trace spans for one specific request or run, use the bounded
local observability projection with a correlation filter, e.g.
`GET /v1/observability/trace?correlation_id=<id>`; `category` and `severity`
filters compose the same way.

## Internet access

For Internet use, put the worker endpoint behind a VPN, or behind a TLS
reverse proxy that restricts source IPs to the coordinator (in static mode the
coordinator sends no credentials, so the proxy cannot authenticate it), plus
firewall allow-listing. Never port-forward Ollama's raw
`11434` port or Sonder's loopback service. The coordinator's API authentication
and the worker's TLS boundary are separate controls.
