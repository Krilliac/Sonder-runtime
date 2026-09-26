# Configuration

Two layers, deliberately separate:

1. **Production configuration** (`sonder_config.py`) — typed, validated,
   **fail-closed**, read once at startup. Governs bind, auth, TLS, limits,
   state paths, features, capacity, observability, backups.
2. **Runtime policy** (`runtime_policy.py`) — hot-reloadable JSON that
   selects model aliases, routing lanes, and NPU modes. It can **never**
   widen network, filesystem, credential, or cloud permissions.

## Precedence (production config)

Lowest to highest:

```
built-in safe defaults
  < profile TOML (/etc/sonder/sonder.toml)
  < secrets env file (/etc/sonder/sonder.env, mode 0600)
  < process environment (SONDER_* / OLLAMA_*)
  < explicit --set command-line overrides
```

Validation collects **all** errors and reports them together; no listener
opens until every mandatory check passes. Secrets are **never** accepted
in TOML — a secret key there is a validation error.

```bash
python -m sonder_runtime config --config /etc/sonder/sonder.toml \
    --secrets /etc/sonder/sonder.env      # prints the effective, redacted config
```

## `sonder.toml` (see `packaging/sonder.toml.example`)

```toml
schema_version = 1
profile = "server-private"          # or "workstation-local"

[server]
host = "127.0.0.1"                   # non-loopback needs the two rules below
port = 11435
auth_mode = "api-key"               # api-key | account | both | either
max_request_bytes = 1048576
max_concurrent_requests = 4
request_timeout_seconds = 300
tls_terminated_by_proxy = true      # reference TLS-proxy deployment; also hides the local log dashboard
trusted_proxy_cidrs = ["127.0.0.1/32", "::1/128"]  # X-Forwarded-For is read only with tls_terminated_by_proxy = true
allowed_hosts = []                  # extra Host names ("name" or "name:port"); only local-open refuses other names (421)
work_wait_seconds = 240             # routed chat work: wait before answering with a work-run id
work_budget_seconds = 1800          # routed chat work: wall-clock budget, then effects are refused
work_max_running = 2                # concurrent routed work runs
stream_heartbeat_seconds = 15       # SSE keep-alive interval while a streamed turn generates

[state]
home = "/var/lib/sonder"
workspace_roots = ["/srv/sonder/workspaces"]
minimum_free_disk_bytes = 5368709120

[ollama]
url = "http://127.0.0.1:11434"
allow_remote = false                # remote-Ollama consent gate

# Optional multi-PC inference pool. Keep model tags installed on every host.
# The environment form is preferred for deployment secrets and overrides:
# SONDER_OLLAMA_WORKERS=https://192.168.1.20:11434,https://192.168.1.21:11434
workers = []
worker_max_inflight = 1             # coordinator cap per host
worker_queue_depth = 32             # bounded waiters across the pool
worker_admission_timeout_ms = 1000
worker_failure_threshold = 3
worker_cooldown_seconds = 30
worker_capability_ttl_seconds = 300
worker_probe_timeout_ms = 2000

[compute]
allow_remote = false               # private-node compute consent gate
node_id = "local"
snapshot_ttl_seconds = 30
probe_timeout_ms = 2000
# Add [[compute.nodes]] and worker-owned [[compute.jobs]] only after following
# docs/runbooks/compute-fabric.md. Remote jobs also require per-request consent.

[features]
cloud = false                       # hosted-model consent gate
web = false                         # web tools consent gate
training = false
npu = false

[capacity]
http_requests = 4
queue_depth = 32
# ... fleet_workers, autopilot_runs, training_jobs, etc.

[observability]
log_format = "json"
metrics_enabled = true
audit_retention_days = 90

[backup]
enabled = true
target = "/var/backups/sonder"
retention_daily = 7
retention_weekly = 4
retention_monthly = 6
```

### The bind-security rule

A non-loopback `[server].host` is **rejected before bind** unless *both*
`tls_terminated_by_proxy = true` and a `SONDER_API_KEY` of ≥24 chars are
set. The reference topology keeps Sonder on loopback behind a TLS proxy
regardless; it still sets `tls_terminated_by_proxy = true` so the
unauthenticated loopback-only log dashboard is not made available through the
proxy. Set it to `false` only for a direct local development listener. See
[Security Model](09-security-model.md).

### Bounded fixed-peer fact replication

`[memory_replication]` is disabled by default. When enabled, it configures a
single exact project-scoped `fact` stream for one trusted private cluster. It
does not enable general memory migration, control-state replication, automatic
retry, discovery, enrollment, sharding, quorum, takeover, failback, or high
availability.

```toml
[memory_replication]
enabled = false
local_node_id = ""
project_scope = ""
receiver_enabled = false
accepted_source_ids = []
request_timeout_seconds = 5
max_request_bytes = 8388608
max_response_bytes = 65536
max_batch_records = 256
# peers = []
#
# [[memory_replication.peers]]
# node_id = "node-b"
# project_scope = "project-a"
# origin = "https://<PEER-DNS-NAME>:8443"
```

An enabled section requires a nonempty bounded local identity, one exact
project scope, at least one and at most 16 fixed peer records, and a canonical
credential-free HTTPS origin with an explicit port for every peer. A peer
scope must exactly match the local scope. A receiver can accept only an
explicit bounded tuple of source IDs that are already fixed peers. Environment
and `--set` inputs cannot select this topology. A receiver must remain
loopback-only or be behind the declared TLS proxy; the direct peer client
rejects redirects and disables ambient proxies.

The two required replication secrets are environment-only and are separately
redacted:

```text
SONDER_MEMORY_REPLICATION_KEY=<shared-current-peer-bearer>
SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY=<different-local-only-integrity-key>
```

Each must be a distinct printable 32–512 character value. The peer bearer
must differ from the API, artifact-transfer, and auth secrets. The local
state-integrity key must differ from every one of those values and is never
sent to a peer. A one-sided peer-key change is rejected by the receiver; no
overlap or automatic key rotation exists. The complete operator procedure,
two-host acceptance template, rollback boundary, and honest single/two-PC
limits are in [Bounded, explicit fact replication](../runbooks/memory-replication.md).

## Secrets file (`packaging/sonder.env.example`, mode 0600)

```
SONDER_API_KEY=...            # bearer key clients present
SONDER_AUTH_SECRET=...        # HMAC secret for account tokens
```

The loader refuses a group/world-readable secrets file on POSIX. Rotate
with `python -m sonder_runtime rotate-key` (overlap window; old key valid
until expiry). See [rotate-credentials](../runbooks/rotate-credentials.md).

## Environment variables (compatibility surface)

Core paths/identity: `SONDER_HOME`, `SONDER_DB`, `SONDER_AUTOPILOT_DB`,
`SONDER_FLEET_DB`, `SONDER_OPERATIONS_DB`, `SONDER_UPDATES_DB`,
`SONDER_APPROVALS_DB` (one-shot permission approvals), `SONDER_TOOL_AUDIT`
(the typed tool gateway's durable receipts), `SONDER_RUNTIME_POLICY`,
`SONDER_TRAINING_STATE`.

Formal verification: `SONDER_LEAN_EXE` selects Lean 4. Optional
`SONDER_LAKE_EXE` plus `SONDER_LEAN_PROJECT` select a pinned Lake project so
proofs can import Mathlib. These paths configure already-installed local tools;
the verifier never downloads a toolchain or dependency. Without overrides, the
verifier applies the repository `lean-toolchain` pin and verifies the discovered
Lean reports the pinned version.

Serving/auth: `SONDER_API_KEY`, `SONDER_HOST`, `SONDER_PORT`,
`SONDER_AUTH_MODE`, `SONDER_AUTH_SECRET`, `SONDER_MAX_REQUEST_BYTES`,
`SONDER_MAX_CONCURRENT_REQUESTS`, `SONDER_QUEUE_DEPTH`, `SONDER_CORS_ORIGINS`,
`SONDER_ALLOWED_HOSTS` (comma-separated `[server].allowed_hosts`),
`SONDER_HTTP_WORK_WAIT_SECONDS`, `SONDER_HTTP_WORK_BUDGET_SECONDS`,
`SONDER_HTTP_WORK_MAX_RUNNING`, `SONDER_STREAM_HEARTBEAT_SECONDS`.

Orchestration: `SONDER_MAX_WORKER_CAP` lowers the absolute ceiling for explicit
per-run `worker_cap` requests. It accepts a positive decimal integer only and
is clamped to the compiled ceiling of 64. Invalid, boolean, fractional,
non-finite, zero, or negative values fail safe to the historical 16-worker
ceiling and are visible in `master_capacity`. Without a per-run `worker_cap`,
the conservative hardware-derived worker width is unchanged.

Physical model request rate: set both `SONDER_MODEL_REQUEST_BURST` (1–256) and
`SONDER_MODEL_REQUESTS_PER_MINUTE` (1–1200), or leave both unset to disable this
optional limit. The burst is shared by Ollama and OpenAI-compatible chat and
embedding sends, including provider retries. Admission is transactional across
processes using the same `SONDER_HOME` and survives process restarts. Different
state homes have independent limits. If live processes disagree on settings,
the shared store keeps the lower burst and refill rate; raising either limit
requires a deliberate reset of both `model-request-admission.sqlite3` and its
`.lock` marker after all senders have stopped. A missing database after first
use, or a missing/damaged bucket row or marker, refuses sends until repaired.
The persistent marker prevents a missing database from looking like first boot
after a process restart; these guards do not isolate state from arbitrary
same-user host code.

Agent batching guard: within one agent turn, distinct single `file_read`
calls get an advisory pointing at `context_pack` after
`SONDER_AGENT_BATCH_ADVISORY_AFTER` targets (default 3, range 2–19). New-target
singles are refused, without being dispatched, after
`SONDER_AGENT_BATCH_REFUSE_AFTER` targets (default 6, from the advisory value
to 19). The run ends at the `SONDER_AGENT_BATCH_MAX_REFUSALS`-th refusal in
one window (default 3, range 1–10); a successful `context_pack` or a state
change starts a new window and a new refusal count. The ceiling of 19 keeps
every accepted value able to fire within the 20-step turn clamp. An invalid
value logs a warning and keeps all defaults; the guard cannot be switched
off. It never applies to mutating or execution tools, and never refuses a
retry of a failed read or a read of a file a successful `context_pack`
returned.

Consent gates: `SONDER_ALLOW_CLOUD`, `SONDER_WEB_TOOLS`,
`SONDER_ALLOW_REMOTE_OLLAMA`, and `SONDER_ALLOW_REMOTE_COMPUTE`. These are
independent: enabling private-node compute does not enable remote inference or
hosted models. Remote compute also requires per-workload consent. See
[Private Compute Fabric](../runbooks/compute-fabric.md). To add independent Ollama hosts to the local
inference pool, set `SONDER_OLLAMA_WORKERS` to a comma- or semicolon-separated
list of origins, for example:

```text
SONDER_ALLOW_REMOTE_OLLAMA=1
SONDER_OLLAMA_WORKERS=https://192.168.1.20:11434;https://192.168.1.21:11434
```

The coordinator keeps its normal `[ollama].url` endpoint as the first worker,
then schedules requests by least in-flight count, breaking idle ties toward
the host with the lowest observed request latency. A worker is temporarily
circuit-broken after three transport failures; repeated trips double the
cooldown up to 8x, and a worker returning from cooldown receives one trial
request at a time until a success closes the circuit. Requests fail over only
before any response is received. Every host must have the selected model tag
installed; Sonder does not copy model weights between PCs. Remote origins must
use HTTPS and must not put credentials in the URL. For a private Ethernet or
Wi-Fi link, use a private CA/VPN or a TLS reverse proxy; do not expose raw
Ollama or Sonder loopback ports to the Internet.
`SONDER_ALLOW_REMOTE_OLLAMA`, `SONDER_FILE_ROOTS`, `SONDER_LOCATION_CONSENT`,
`SONDER_EXPOSE_REASONING`, `SONDER_ALLOW_PRIVATE_COT`.

`SONDER_EXPOSE_REASONING` decides whether Sonder asks a reasoning model for its
thinking at all; with it off nothing is captured, so nothing can be shown. It
also means the model is asked to think, which costs latency and tokens.
`SONDER_ALLOW_PRIVATE_COT` is separate and narrower: it decides whether
`admin_private_chain_of_thought` (`/cot`) may reveal that record, and it is
**not sufficient on its own** — the tool also requires an explicit `allow` rule
for its own name in `permissions.json`, since the built-in rule denies it.
Write that rule with `permission_rule_set` or by hand; what the gate requires
is the state on disk, not any one route to it. Both acts are required; either
alone still refuses.

Models/tiers: `SONDER_FAST`, `SONDER_CODE`, `SONDER_GENERAL`,
`SONDER_REASONING`, `SONDER_VISION`, `SONDER_BASE_MODEL`,
`SONDER_EMBED_MODEL`, `SONDER_CONTEXT_SIZE`, `SONDER_SESSION_NUM_CTX`,
`SONDER_NATIVE_CONTEXT_MAX`, `SONDER_VIRTUAL_CONTEXT_MAX`, `SONDER_LEARN_TIERS`
(see [Context sizing](#context-sizing) below; `SONDER_KV_CACHE_TYPE`, or
failing that `OLLAMA_KV_CACHE_TYPE`, also affects the default).
`SONDER_REASONING` / `SONDER_VISION` also accept `none` (or `off`) to leave
that specialist tier unbound, in which case reasoning/vision work falls back
to a base tier.
`SONDER_MODEL_ESCALATION` (default on; `0`/`off` disables) lets the default
route step up to the next distinct bound local model when its first model
fails or answers nothing, at most twice per turn; explicit tiers and model
pins never move ([Tiers & Gateway](08-model-tiers-and-gateway.md)).

### Sonder ecosystem (Inference provider and Observatory)

Sonder Inference provider (read lazily per call; reference:
[sonder-inference-provider.md](../architecture/sonder-inference-provider.md),
procedure: [runbook](../runbooks/sonder-inference.md)):

| Variable | Default | Meaning |
|---|---|---|
| `SONDER_MODEL_BACKEND`, `SONDER_<TIER>_PROVIDER` | `ollama` | `sonder-inference` (also `sonder_inference`, `sonder-infer`, `inference`) selects the provider; `sonder` is refused |
| `SONDER_INFERENCE_BASE_URL` | `http://127.0.0.1:11437` | `sonder-infer serve` origin |
| `SONDER_INFERENCE_READY_FILE` | unset | `url` of a `serve --ready-file`, used only without a base URL |
| `SONDER_INFERENCE_MODEL` | `default` | model id when no tier mapping applies |
| `SONDER_INFERENCE_TIER_MODELS` | unset | e.g. `fast=a,general=b` |
| `SONDER_INFERENCE_API_KEY` | unset | bearer token; redacted from logs |
| `SONDER_ALLOW_REMOTE_INFERENCE` | `0` | `1` permits a non-loopback URL, which also needs `https://`, a key, and a cloud-allowed context |
| `SONDER_INFERENCE_TIMEOUT_SECONDS` | `300` | per-call ceiling, never beyond the operation deadline |
| `SONDER_INFERENCE_HEALTH_TTL_SECONDS` | `5` | health-cache lifetime |
| `SONDER_INFERENCE_FALLBACK` | `none` | `ollama` sends requests Inference never received to local Ollama once |

Observatory live export (contract section 11; served by the runtime telemetry
routes, which the chat-telemetry change adds): `SONDER_OBSERVATORY_EXPORT`
(default `1`; `0` makes the telemetry routes answer 404),
`SONDER_OBSERVATORY_BUFFER` (default `4096`, clamped to 256..65536),
`SONDER_OBSERVATORY_MAX_SUBSCRIBERS` (default `8`); typed equivalents under
`[observability]` are `live_export`, `live_export_buffer` and
`live_export_max_subscribers`. `SONDER_CORS_ORIGINS` keeps no default. The
Flutter app reads `SONDER_OBSERVATORY_BIN` to find the Observatory
executable. Inference's own flags (`--port`, `--cors-origin`,
`--token-file`, ...) are documented by `sonder-infer serve --help`.

### Context sizing

Sonder distinguishes the **requested** (virtual) context you ask for from the
**native** `num_ctx` it actually hands Ollama. Values above native are covered
by summaries, retrieval, and facts standing in for turns that no longer fit in
the raw prompt (see the `/context` note at the end of this section for that
live, per-session budget — a related but different thing from the sizing
policy below).

The baseline, before any per-model adjustment, is `default_context()`:
`32768` if the server's KV cache is quantized (`q8_0`, `q4_0`, `q4_1`,
`q5_0`, `q5_1`), else `8192` for full-precision (fp16) KV.

The KV cache type is a setting of the **Ollama server process**, which Sonder
cannot query. Declare it with `SONDER_KV_CACHE_TYPE` (`f16`, `bf16`, `f32`, or
a quantized type above). Without a declaration Sonder falls back to its own
`OLLAMA_KV_CACHE_TYPE`, which is only correct when Sonder launched Ollama
itself; a tray app, system service, or remote host has its own environment.
An unknown or missing value is treated as `f16`. `/contextsize` (the
`context_policy_status` tool) shows the type in use and where it came from
(`declared`, `client-environment`, or `default`), and the auto-sizing plan
records the same pair. Set
`SONDER_CONTEXT_SIZE` or `SONDER_SESSION_NUM_CTX` (equivalent; the first wins
if both are set) to override that baseline explicitly — either accepts a bare
integer or a `k`/`m` suffix (`8192`, `32k`, `1m`).

For any request that does not pin its own `num_ctx`, that baseline (default or
env-overridden) is then auto-sized per loaded model
(`context_policy.auto_context`): 24B+ parameter local models are capped to at
most `16384`, 12B–24B to at most `24576` — tighter than the KV-cache default
regardless of `SONDER_CONTEXT_SIZE` — and the result never exceeds the
model's own advertised context window either way. Smaller models are
unaffected by this cap. A request that *does* pin an explicit `num_ctx` (the
REPL's `/contextsize <size>` / `/ctxsize <size>`, or the `set_context_size`
tool) bypasses auto-sizing entirely and is used as given, subject only to the
ceilings below. That pin is a process-wide default, not scoped to one
conversation — it applies to every session on this running server until
changed again or reset with `/contextsize` with no argument.

Two independent ceilings bound every value above: `SONDER_NATIVE_CONTEXT_MAX`
(default `262144`) caps what is ever sent to Ollama as `num_ctx`, and
`SONDER_VIRTUAL_CONTEXT_MAX` (default `1000000`) caps the requested/virtual
size before it is clamped to native. A requested size below the `512`-token
floor is rejected rather than silently raised.

`/contextsize` (no argument) or the `context_policy_status` tool report the
requested size, the actual native `num_ctx`, both ceilings, and whether the
session is in `native` or `virtual` mode — distinct from `/context`, which
reports the *live* per-session budget (turns, summaries, memory) rather than
this sizing policy.

### Hardware-aware 30B planning

`/hardware` and `hardware_profile` report live free VRAM when the platform
probe can obtain it, but keep driver detection, provider readiness, and model
fit as separate facts. The built-in Qwen3-Coder 30B-A3B planning profile is
equivalent to:

```toml
[models.sonder_30b]
name = "qwen3-coder:30b-a3b-q4_K_M"
quantization = "Q4_K_M"
backend = "ollama"
gpu_layers = "auto"
context_size = 8192
```

Its fit estimate includes quantized weights, KV-cache growth, runtime
overhead, and a safety margin. A 16 GB card therefore receives an explicit
`gpu+ram-hybrid`/fallback warning for this profile; Sonder does not claim full
GPU residency until both the measured fit and backend readiness are present.
The profile is advisory and does not change Ollama settings or download a
model. CPU fallback remains available when GPU state is unknown or unsafe.

Behavior toggles: `SONDER_SPECULATION` (0 disables speculative execution),
`SONDER_METRICS`, `OLLAMA_HOST`.

Update/publish (optional): `SONDER_RELEASES_DIR`, `SONDER_CURRENT_LINK`,
`SONDER_UPDATE_ALLOW_UNSIGNED` (dev only), `SONDER_BRANCH_PREDICTOR`.

`python -m sonder_runtime diagnostics` prints a redacted bundle of the
effective configuration, schema state, and preflight for support.

## One user-global file

For a workstation install, Sonder automatically uses
`%LOCALAPPDATA%\\sonder\\sonder.toml` on Windows (or the platform-specific
Sonder state home elsewhere) when that file exists. The optional matching
secret file is `sonder.env`. `SONDER_CONFIG` and `SONDER_SECRETS`, then the
explicit `--config` and `--secrets` flags, select a different file. This same
resolved configuration is now applied by `serve`, `repl`, and `mcp`; command
line flags remain only explicit, higher-precedence one-shot overrides.
