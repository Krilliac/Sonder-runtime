# Configuration

Two layers, deliberately separate:

1. **Production configuration** (`sonder_config.py`) — typed, validated,
   **fail-closed**, read once at startup. Governs bind, auth, TLS, limits,
   state paths, features, capacity, observability, backups.
2. **Runtime policy** (`runtime_policy.py`) — hot-reloadable JSON that
   selects model aliases, routing lanes, and NPU modes. It can **never**
   widen network, filesystem, credential, or cloud permissions.

## Model lists and defaults

The version 1 catalog in `sonder_runtime/adapters/data/model_lists.json` ships
the model names and routing lists. An optional `<state home>/model_lists.json`
overrides it; `SONDER_MODEL_LISTS` selects another overlay path. The overlay is
a JSON object with `"version": 1`. Lists and scalar values replace the whole
key; dictionary entries merge by subkey, including nested router and training
entries. Unknown keys, invalid names, unknown tiers, and unpinned training
revisions invalidate the entire overlay. The runtime reports the issue, uses
the shipped catalog, and leaves the invalid file untouched. Missing or invalid
shipped data raises a configuration error naming the file; there is no Python
copy of the model defaults. MCP import remains available to report the error.

The catalog keys are `retired_cloud_models`, `legacy_cloud_general_model`,
`default_cloud_models`, `stable_alias`, `default_local_models`,
`default_embedding_model`, `reserved_personal_model`, `default_routing`,
`vision_only_model_families`, `tier_strength`, `working_strength`,
`capability_router` (`preferred`, `ladder`, `fallback`, `keywords`),
`ensemble_skip_tiers`, `bootstrap_models`, and `training_model_specs`.
Training sizes and memory-budget algorithms remain code contracts; each HF
revision in the catalog must be an exact 40-character commit, never a branch.

Existing environment overrides still win: `SONDER_FAST`, `SONDER_CODE`,
`SONDER_GENERAL`, `SONDER_REASONING`, `SONDER_VISION`, `SONDER_CODE_LOCAL`,
`SONDER_CLOUD_CODE`, `SONDER_CLOUD_GENERAL`, and `SONDER_EMBED_MODEL`.
`SONDER_LEARN_TIERS` continues to override the derived learning-tier set.
Runtime policy still owns persisted tier bindings; changing a catalog default
does not replace an operator's existing runtime-policy assignments.

At the console:

```text
/runtime models
/runtime models set default_local_models {"code":"custom-coder:7b"}
/runtime models set bootstrap_models {"small":"custom-coder:1.5b"}
/runtime models reset default_local_models
```

Status shows effective defaults, their `shipped`, `overlay`, or `env` sources,
file paths, and errors. Set/reset accept top-level catalog keys and write only
the overlay, using a file lock and atomic replacement. They require exactly
the same attended console authority as `/runtime tools`; MCP/HTTP/model calls
cannot write the overlay. The overlay, its lock, and atomic temporary siblings
are protected control-plane paths, including an environment-selected path.
Invalid existing overlays must be fixed by hand before console edits.
Loaders notice file identity changes; restart the runtime to apply startup
bindings such as aliases and ensemble exclusions consistently.

Training, deployment, and rollback pin one immutable catalog for the whole
lifecycle operation. Editing the overlay takes effect on the next operation;
it cannot change the alias being validated or published by an operation already
running. Transition journals record the exact personal/rollback aliases and
training model revisions, so recovery uses those recorded identities after a
restart. A pending journal from before identity pinning is left untouched and
requires operator recovery; it never inherits newly configured aliases.

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
web_app_enabled = false             # serve the Flutter web app at /app/ (SONDER_WEB_APP); see "Serving the web app"
web_app_dir = ""                    # absolute bundle root; "" = <state home>/web_app (SONDER_WEB_APP_DIR)
work_wait_seconds = 240             # routed chat work: wait before answering with a work-run id
work_budget_seconds = 1800          # routed chat work: wall-clock budget, then effects are refused
work_max_running = 2                # concurrent routed work runs
stream_heartbeat_seconds = 15       # SSE keep-alive interval while a streamed turn generates
prewarm_on_start = true             # load the default chat model once the listener is bound (SONDER_PREWARM_ON_START); needs prewarm enabled, goes through GPU admission

[state]
home = "/var/lib/sonder"
workspace_roots = ["/srv/sonder/workspaces"]
minimum_free_disk_bytes = 5368709120
auto_workspace = true               # console work with no folder gets a dated folder in default_workspace_root (SONDER_AUTO_WORKSPACE)
default_workspace_root = ""         # "" = %USERPROFILE%\Sonder\workspaces or ~/Sonder/workspaces; granted to managed console work (SONDER_DEFAULT_WORKSPACE_ROOT)

[ollama]
url = "http://127.0.0.1:11434"
allow_remote = false                # remote-Ollama consent gate

# Optional multi-PC inference pool. Keep model tags installed on every host.
# The environment form is preferred for deployment secrets and overrides:
# SONDER_OLLAMA_WORKERS=https://192.168.1.20:11434,https://192.168.1.21:11434
workers = []
worker_max_inflight = 1             # coordinator cap per host
worker_queue_depth = 32             # bounded waiters across the pool
worker_admission_timeout_ms = 1000  # interactive chat/offload fail-fast bound
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

### Serving the web app

With `[server].web_app_enabled = true` (or `SONDER_WEB_APP=1`) the HTTP server
serves the Flutter app's web build at `/app/`, so a browser on the same
network reaches the app without a native install. The setting is off by
default; when off, `/app/` is an ordinary unknown route.

Build and publish the bundle with `python scripts/build_web_app.py` (run it
from a linked worktree; pass `--allow-main-checkout` to build in the main
checkout). It runs `flutter build web` with `--no-web-resources-cdn --csp
--base-href /app/`, writes `sonder-web-manifest.json` (sha256 and size of
every file) and swaps `<web_app_dir>/current.json` atomically, keeping the
newest three releases. The default `web_app_dir` is `<state home>/web_app`;
the script refuses a directory inside the repository. A running server picks
up a new release on the next request. Enabled with no bundle, `/app/` answers
404 `WEB_APP_NOT_BUILT` with this remedy.

Security model:

- **Allowlist.** Only files listed in the verified manifest are served. Every
  file's size and sha256 is checked when a release is first loaded; an
  unlisted `index.html`, a symlink or junction, a dotfile, or any traversal
  form (`..`, encoded separators, drive letters, NTFS streams) is refused.
- **Static shell, no secrets.** The shell is served without credentials
  because a browser navigation cannot send the `Authorization` or
  `X-Sonder-Account-Token` header. Every `/v1` call the app then makes is
  gated exactly as for any other client, and the app signs in through the
  normal account routes. Host policy applies first, so a rebinding name gets
  421 on `/app/` too.
- **Same origin only.** The app talks to the origin that served it and its
  server URL is not editable there. A same-origin request from a trusted Host
  passes the origin check; `cors_origins` is unchanged and no
  `Access-Control-Allow-Origin` header is added. A name that is only accepted
  because credentials are required never gets this exemption.
- **Browser hardening.** Responses carry a strict Content-Security-Policy
  (own origin only; `wasm-unsafe-eval` for CanvasKit, no `unsafe-eval`),
  `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` (not
  `no-referrer`, which would make browsers send `Origin: null` on POSTs),
  cross-origin opener/resource isolation and a denying Permissions-Policy.
  Files get `Cache-Control: no-cache` plus an `ETag`, because Flutter's output
  names are not content-hashed.
- **Credentials stay in memory.** In a browser the app keeps API keys and
  account sessions in memory only, so a reload means signing in again.
  Account sign-in needs HTTPS or a numeric loopback address (existing rule),
  so reach the app over HTTPS through your TLS proxy or on `127.0.0.1`.
- **Private fonts.** The engine's fallback-font download from `gstatic.com`
  is blocked by the policy; glyphs outside the bundled fonts (emoji, CJK) show
  as empty boxes.
- `HEAD` requests are not supported (the server answers 501), as elsewhere.

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
`SONDER_WEB_APP` (`[server].web_app_enabled`), `SONDER_WEB_APP_DIR`
(`[server].web_app_dir`, absolute),
`SONDER_HTTP_WORK_WAIT_SECONDS`, `SONDER_HTTP_WORK_BUDGET_SECONDS`,
`SONDER_HTTP_WORK_MAX_RUNNING`, `SONDER_STREAM_HEARTBEAT_SECONDS`,
`SONDER_PREWARM_ON_START` (`[server].prewarm_on_start`, default on).

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
failing that `OLLAMA_KV_CACHE_TYPE`, also affects the default, and
`SONDER_RESIDENCY_FEEDBACK` controls measured spill correction).
`SONDER_REASONING` / `SONDER_VISION` also accept `none` (or `off`) to leave
that specialist tier unbound, in which case reasoning/vision work falls back
to a base tier.

`SONDER_EMBED_ON_CPU=1` is opt-in. It asks Ollama to keep the embedding model
on the CPU (`num_gpu: 0`). Use it when the chat model nearly fills VRAM:

- **Without it:** a GPU-loaded embedder evicts the chat model, so every memory
  or routing embedding forces a chat-model reload. Measured 2026-09-28 at
  about 20 s for Qwen3.8 27B on a 16 GB card.
- **With it:** the embedder stays resident beside the chat model, at about
  25 ms per embedding.

It pairs with Ollama's `OLLAMA_MAX_LOADED_MODELS` of 2 or more.

`SONDER_EMBED_BASE_URL` sends embeddings to a dedicated Ollama origin, for
example a LAN node, while chat stays on the primary. This keeps the embedder
off the chat model's GPU entirely. The origin passes the same policy as
`OLLAMA_HOST`: a remote host needs `https://` and `SONDER_ALLOW_REMOTE_OLLAMA=1`.
If the host is unreachable, or a fronting proxy answers 502-504, embeddings
stop for `SONDER_EMBED_COOLDOWN_SECONDS` (default 30). During that time
callers use their lexical fallback. `SONDER_EMBED_FALLBACK=local` instead
retries on a loopback primary with the model forced onto the CPU, and
`fallback_reason` records it. The model must also be installed there. With
`OLLAMA_MAX_LOADED_MODELS=1` that fallback still evicts the chat model.
`SONDER_EMBED_KEEP_ALIVE` (for example `24h`, or `-1` for no expiry) sets
Ollama's `keep_alive` on embedding requests. Use it so a busy remote host does
not unload the embedder when it is idle. `SONDER_EMBED_NUM_CTX` (for example
`4096`; values below 256 are ignored) sets `num_ctx` on embedding requests so
the embedder's runner is not sized to the host's default context (32k on a
large GPU budget turns a 4 GB embedder into a 10.8 GB runner).
The doctor's `sonder_inference_gpu` check does not count a remote embedder as
a GPU contender, and reports ok when `OLLAMA_LLM_LIBRARY=cpu` pins the local
Ollama daemon to the CPU library (it names what that setting keeps off the
GPU).
Loopback-only features (semantic tier routing, `memory_embedding_backfill`,
the learning-health revision refresh) stay off while the embedder is remote.
`sonder doctor` reports whether the embedding model is installed where
embeddings are sent. See
[Multi-PC Ollama](../runbooks/multi-pc-ollama.md#dedicated-embedding-host).

`SONDER_KEEP_PRIMARY_RESIDENT=1` (opt-in) sends `keep_alive: -1` for the
primary chat model only, so it is not unloaded between turns; use it when that
model is the only local model on the GPU (doctor `sonder_inference_gpu` warns
otherwise). Streaming, thinking and sampling switches for Sonder Inference:
[request path](../integration/sonder-inference-request-path.md).
`SONDER_MODEL_ESCALATION` (default on; `0`/`off` disables) lets the default
route step up to the next distinct bound local model when its first model
fails or answers nothing, at most twice per turn; explicit tiers and model
pins never move ([Tiers & Gateway](08-model-tiers-and-gateway.md)).

#### Long-context overflow

Opt-in (off by default). A dense local model that is fast at short context
can spill out of VRAM and slow sharply past roughly 32k tokens, while a
mixture-of-experts model keeps its speed there. With the overflow on, an HTTP
chat turn whose estimated prompt exceeds the threshold moves from its local
tier (`sonder`, `fast`, `code`, `general`, `reasoning`) to the overflow model
on the Ollama worker pool, so it runs on whichever worker advertises that
model, and the context window is raised to hold the prompt. Cloud targets,
exact model pins and the vision tier never move. The estimate is the
context-health one (about four characters per token) over the history and
the prompt; the system prompt and retrieval augmentation are not counted, so
it is a floor.

The runtime policy holds the setting (`long_context_overflow` in
`runtime_policy.json`; `enabled`, `threshold_tokens` 4096..1048576 default
32768, `model`, `provider` which must be `ollama`). An empty `model` means the
bound reasoning tier's model. Cloud models are refused.

| Control | Effect |
|---|---|
| `/runtime overflow status` | show the settings in force (also a line of `/runtime status`) |
| `/runtime overflow on` / `off` | enable or disable it |
| `/runtime overflow threshold <tokens>` | set the trigger |
| `/runtime overflow model <local-model>` | set the model (`model` alone: reasoning tier) |
| `SONDER_LONG_CONTEXT_OVERFLOW=1` | override `enabled` for this process |
| `SONDER_LONG_CONTEXT_THRESHOLD`, `SONDER_LONG_CONTEXT_MODEL` | override the threshold and model; an invalid value is ignored and reported |

Every overflow decision is stated, never inside the answer text: the
response receipt carries `sonder_receipt.overflow` (`status` `switched` or
`unavailable`, `notice`, models, `worker`, `estimated_tokens`,
`threshold_tokens`), the app shows the notice as a chip on the message, the
activity record gets a `model_route` event, the system prompt names the
overflow model, and the Observatory receives `route.changed` with
`reason_code: context_over_threshold`. Notices read, for example,
`long-context overflow: switched to qwen3.6:35b on 10.77.0.2:8443 — context
41.2k tokens > 32.8k threshold`, or `long-context overflow (qwen3.6:35b)
unavailable, stayed on <model>: <reason> — …` when no pool worker advertises
the model or the overflow attempt fails (the turn then falls back to its
original route).

#### Context-fit routing

Always on, and a no-op until a turn is known not to fit. An HTTP chat turn
estimated above 4096 tokens is checked against the context length of the
model it is about to be sent to: a Sonder Inference tier's launch-profile
`context_length` (from `/v1/models`), an Ollama tier's configured `num_ctx`,
else the Ollama model's own context length. A turn that fits, or whose
context length is unknown, routes exactly as before. A turn that does not
fit moves to the long-context tier when one is set (stated in the receipt
with `route: "context_fit"`, a `route.changed` event with `reason_code:
context_exceeds_model`, and an activity entry), and otherwise fails before
any model call with an error that says how to fix it; nothing is truncated.
The opt-in overflow above is decided first.

| Control | Effect |
|---|---|
| `routing.long_context_tier` in `runtime_policy.json` (`runtime_policy_update routing_json='{"long_context_tier": "reasoning"}'`, `""` clears) | the local tier a too-long turn moves to; unset by default |
| `SONDER_LONG_CONTEXT_TIER=<tier>` | override it for this process; an invalid value is ignored and reported |
| `SONDER_OLLAMA_TIER_OPTIONS='{"code": {"num_ctx": 32768}}'` | an Ollama tier's request window (512..1048576), also the length the check measures it against; an explicit request context size still wins |
| `SONDER_INFERENCE_PROFILE_TTL_SECONDS` | how long `/v1/models` profile metadata is cached per endpoint (default 60, 0..3600) |

`runtime_policy_status` lists the long-context tier and each tier's
effective context. Details and an example:
[context-fit routing](../integration/context-fit-routing.md).

#### Tool-lane model deny list

Some installed models are text-only advisers: they may be chatted with by
name, and used through text-only `ensemble_answer` and `consult`, but any
lane that calls tools, or executes or applies model output (agent and
workbench turns, repository and build fleet workers, campaigns, game generation,
`codegen_build_loop`, `improve_function`, `parallel_generate_run`,
`parallel_generate_run_languages`, `mesh_generate` (its script writer and its
vision critique, which steers the next round's script), and the
self-modification job run by `scripts/nightly_self_improve.py` and
`scripts/selfmod_forever.py`),
refuses them, the chat code gate never runs their replies, and they are never
bound to a tier. The self-modification job checks its model (`--model`, else
the `code` tier) before it starts a run and stops with `model refused, no run
started: ...`. The shipped list (the "floor", see below) is the `astra-cyber-`
prefix and two source repositories. For each source, the hosted `hf.co/owner/repo` and
`huggingface.co/owner/repo` ids, the hostless `owner/repo` id, and the repository
name without `-GGUF` (the model stem) are denied as prefixes. A floor stem
matches at the start of any path segment, followed by any characters, including
another format or quantisation suffix. The `astra-cyber-` prefix likewise
matches at the start of any segment. Both `/` and `\` separate segments;
matching casefolds and trims names and removes one trailing `:latest`. An
`https://` URL or registry prefix cannot hide a source. A different model
stored under a directory named after a blocked model is also refused.
Policy-added names and prefixes are matched against the full name, its last
`/` segment (backslashes included, as before), and its last segment after
either separator.

The model lists live in two JSON files, not in Python:

- **Shipped defaults**, `sonder_runtime/adapters/data/model_tool_policy.json`
  (schema `version` 1, shipped beside `semantic_tier_head.json` and versioned with
  the code). It holds `model_profiles`, each `{"id", "floor", "match": {"names",
  "prefixes"}, "tools", "lanes"}`, and `undetermined_served_models` (served-model
  ids that name no model: `""` and `default`). The `security-advisers` profile is
  a floor (`"floor": true`) with `tools: []`, meaning chat by name only, and
  every prefix spelled out. Entries are lowercase; the loader casefolds them
  anyway.
- **Operator overlay**, `<state home>/model_tool_policy.json` (the path in
  `SONDER_MODEL_TOOL_POLICY` instead, when set), same schema, optional. It is a
  separate file from `runtime_policy.json` so that `runtime_policy_update` can
  never reach it. Edit it by hand, or at the console with `/runtime tools`
  (below); that editor is its only writer in Sonder (see "Protected from the
  file tools" below). It can add names and prefixes (in
  a new profile, or in a floor profile's `match`), add `tools` and `lanes` to
  any profile (a floor included), add served-model ids that name no model and
  add `restricted_secret_path_globs`. It cannot set `"floor"`, cannot set
  `restricted_tool_ceiling`, and cannot remove or narrow anything shipped: the
  floor's names and prefixes, shipped tools and shipped globs are always in
  force whatever the overlay says (the overlay's entries are unioned in). A
  profile that matches no model, an unknown key, a duplicate `id`, a bad name
  or glob or a file over 1 MiB makes the overlay invalid.

Protected from the file tools. Both files are control-plane state, and these
tools refuse to change them (the overlay with its `.lock` and `.tmp-`
siblings; the shipped file and, without a developer token, the whole package
data directory holding it):

- `file_write`, `file_edit`, `file_copy`/`file_move`, `file_delete`, batch
  writes and the patch tools, from an agent turn or MCP. A developer token
  does not open the two policy files (or a recursive delete of a folder that
  holds one): only the attended `/runtime tools` editor writes the overlay,
  and nothing at run time writes the shipped file. The token still opens the
  rest of the package data directory and the other control-plane state. A
  developer-token recursive delete removes a symlink or junction itself and
  never follows it into another folder.
- `fetch_artifact`: a destination, staging file or provenance sidecar there.
  `archive_extract` refuses a destination directory that would hold a policy
  file (a missing overlay included).
- `apply_patch`: it lists every path a patch would write (`git apply
  --numstat`, both names of a rename) and refuses the patch when any is
  protected, in any repository, attended or not.
- `rename_symbol`: every file it would rewrite gets the file tools' check, and
  one protected file refuses the whole rename (the preview still runs).
- `format_code` and `lint_run` with `fix`: the formatter is not run when its
  path (or the root) is or holds a protected place (the workspace root, the
  package data directory, Sonder's own code directories, the overlay, the
  Sonder home's control files) or lies inside one, when a symlink or junction
  under it leads to one, or when it holds a hard link to a protected file. The
  folder is scanned for hard links only when a protected file has more than
  one name, so a pnpm store or a uv environment is not refused. A check-only
  run is unchanged. These path checks are defence in depth: a formatter runs a
  program, so it is an execution-risk tool (below).

The live runtime checkout. The checkout Sonder runs from (the repository
holding the workspace root, the package data directory, the overlay or the
Sonder home) is never changed by a model. Production is fast-forwarded from
origin/main and self-modification has its own reviewed pipeline, so in that
checkout `git_checkout`, `git_branch`, `git_commit`, `git_tag`, `git_stash`
push, pop and drop, `git_merge`, `git_cherry_pick`, `apply_patch` (without
`check_only`), `format_code` and
`lint_run` with `fix` refuse unless the operator typed that very tool at the
console (`/git_checkout ...`) with a person present. The rule does not try to
predict what a call would write or which commit a ref names: it looks at the
paths, asks `git rev-parse` which repository git would use there, and looks at
the attendance signal
(`adapters/operator_attendance.py`), which the console dispatcher sets only
around the tool the operator typed, so a model loop started by `/agent` is not
attended and neither is a piped or scripted console. No token opens it.

What counts as live is what git would act on, not the nearest `.git`
(`adapters/live_checkout.py`). Any path inside the live tree is live, whatever
`.git` markers sit in between: git skips a `.git` folder whose `HEAD` is not a
ref and keeps walking up, and a repository nested in the live tree is still
inside it. A path outside the tree is live when a git directory it reaches
is, or shares state with, the live one: a `.git` file whose `gitdir:` names
the live git directory, a git directory whose `commondir` is the live one
(unless it is a registered worktree off the live branch, below), or a
`core.worktree` that points into the live tree or at a folder containing it.
A junction, symlink or different spelling of the live checkout is still the
live checkout (identified by directory identity, not by name).

The rule fails closed. Every file it reads (`.git` files, `commondir`, `HEAD`,
loose refs, config) is read as present, absent or unknown; a file over 1 MiB,
a NUL byte, bytes that are not UTF-8, a config with a BOM or a continuation
line that mentions `worktree`, a drive-relative path or any error other than
"no such file" counts as unknown, and unknown is live. When the live checkout
itself cannot be located (a control-plane location raises, or its git
metadata cannot be read), every path is live and the refusal says so. Git's
own answer is a second check: if `git rev-parse`, run in that folder, names a
live git directory or a work tree inside or containing the live tree, or fails
with anything but "not a git repository", the path is live. That costs two
short git runs (about 20 ms) only for a path the file rule does not already
call live.

A repository that is not the live checkout is unaffected: a user's project,
including repositories nested in it, and a git worktree of the runtime outside
the live tree (its `.git` is a file naming its own `HEAD` and index) keep every
git tool as plain git. A worktree counts only when it is registered (its git
directory is `<live>/.git/worktrees/<name>` and that entry's `gitdir` names
its very `.git` file) and its `HEAD` is not on the branch the live checkout has
checked out; a hand-made git directory sharing the live one is live whatever
its `HEAD` says. A worktree shares branches, tags and the stash with the
live repository, which is why `/runtime_source_stash pop` (and `/stash pop`)
needs the operator at the console and restores only a stash that
`runtime_source_stash save` recorded in the live checkout. It applies that
stash once: if the drop that follows fails, a second pop reports it instead of
applying it again. For the same reason `git_stash pop` and `git_stash drop`
refuse, in any worktree, when the top stash entry is that recorded recovery
stash.

The shipped file is pinned. The first valid read of the shipped policy (the
module reads it as the process starts) records its SHA-256. A later read that
finds other bytes makes the floor unavailable, so every tool-using lane
refuses every model, exactly as for a missing file, until Sonder is restarted
or the operator runs `/runtime tools reload` at the console. `reload` pins
what is on disk and prints the old and the new hash and everything that
differs (floor names and prefixes, the restricted tool ceiling, the secret
path globs, the undetermined served names and each profile's tools), so
compare it with the committed file before trusting it
(`git diff` on `model_tool_policy.json`). Writing the pinned bytes back
restores the floor without a reload, and a rewrite with identical bytes is not
a change. `runtime_source_update` and a reviewed self-modification deploy
change the shipped file on purpose; after either, the floor stays unavailable
until a restart or a `reload`. An observation filter already running for a
granted turn withholds everything once the shipped file stops matching its pin
or the overlay turns invalid.

Other names for the same file count. A Windows `\\?\` spelling is compared in
its ordinary form (a namespace with no ordinary form, such as `\\.\` or
`GLOBALROOT`, counts as protected), a UNC spelling of a local folder and a hard
link are compared by file identity, and no file tool writes in place to a file
that has more than one hard link without a developer token. On Windows every
file tool (reads included) refuses the caller's path when a component ends in
a dot or a space, or holds `:` after the drive (`overlay.json.`, `overlay.json `,
`overlay.json::$DATA`), because Windows treats those as the trimmed name; `.`
and `..` components are fine. A name that starts like the overlay
(`model_tool_policy*`, or the stem of an overlay renamed with
`SONDER_MODEL_TOOL_POLICY`) in the overlay's folder or the state home is
refused for create, write, move, copy and delete, with or without a developer
token.

Not covered: the tools that run programs (`build_run`, `test_run`,
`dependency_add`/`remove`/`update`, `build_clean`, `run_code` and the rest)
can change any file their program writes; they are execution-risk tools
(`permission_modes.risk_of`), gated as such, not by these path checks. The
shipped-file pin is what keeps the floor in force when such a write succeeds,
for the life of the process.

The shipped file also holds `restricted_tool_ceiling` (the only tools any
profile may ever grant: `text_search`, `repository_symbol_index`,
`file_read_range`, `directory_tree`, `context_pack`) and
`restricted_secret_path_globs` (`.env`, `.env.*`, `*.pem`, `*.key`, `*.pfx`,
`*.p12`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `.ssh/**`, `**/secrets/**`,
`*credential*`, `*token*`, `*secret*`, `.git/config`, `.netrc`, `.npmrc`,
`.pypirc`).

##### Restricted tool grants

A model that a profile with `tools` matches is *restricted*: still refused in
every tool lane and still chat by name everywhere, except in the lanes the
profile lists (`agent`, the `agent` tool; `workbench_agent`, `workbench_agent`
with an explicit tier), where it may drive only the granted tools. Every
profile match keeps the model on the deny list, so a grant never opens any
other lane (`codegen_build_loop`, `mesh_generate`, campaigns, the code gate, the
default route, a tier binding, fan-out and the rest refuse it exactly as
before). A profile with `tools: []` (the shipped `security-advisers`) is chat by
name only, as it always was.

A model is restricted only when all of these hold: the caller named it (by
name, not through a tier, a prepared target, a bridged provider or the default
route); the deny list matches it; no profile with no tools, no overlay `deny-`
entry and no legacy `tool_lane_denied_models` entry matches it; every matching
grant lists the lane; and the intersection of the matching grants' tools is
not empty. An unreadable policy file, an unreadable overlay or a missing
shipped file grants nothing. The `agent` entry is opened only by a direct
`agent` call and `workbench_agent` with an explicit tier; a model-planned
`loop` action, `/work` on tier `auto` (its escalation rungs), chat file routing
and a prepared plan never open it.

A tool is granted only when it is in the shipped ceiling *and* passes a code
check: its risk grade is `safe`; it does not reach the network, read or change
memory or learning state, operate the system, or start a program (`run_*`,
`*_run`, `build_*`); and it is a read-only, project-confined tool the agent
lane dispatches. A tool that fails is dropped and `/runtime status` shows a
`DROPPED` line with the reason (the overlay is otherwise applied). Granting
also needs at least one secret glob.

A restricted run is read-only, with no web or location access, no host
profile or brief, no playbook memory and no speculative tool calls. It is
offered only the granted tools (the caller's `allowed_tools`, if any,
intersected with them) and every call is checked at dispatch, so a forged call
to any other tool is refused. It needs a project: the caller must pass
`project=` naming an existing directory (a missing project or a memory-project
label is refused: "a restricted model needs a project root"). Every path
argument is resolved against that root and refused when it leaves it or
matches a secret glob (case-insensitive; a glob without `/` matches any path
component, one with `/` any tail of the path); so is a search `glob` that
selects secret files and `include_hidden` / `include_ignored`. `text_search`,
`repository_symbol_index` and `directory_tree` results are filtered before the
model, the activity record or the caller sees them: a row or tree entry naming
a secret path (as spelled or after following links), an entry inside a
directory that matches a glob, or a line that does not parse, is withheld and
counted. Tree walks never follow a symlink, a junction or any other name-surrogate
reparse point (one whose tag cannot be read counts as a link); other reparse points,
such as cloud-file placeholders, are walked as ordinary content.

The grant is decided again during the turn: at every model call the files must
still grant every tool the turn was opened with (revoking any one stops the
next call), and at every dispatch the tool must still be granted. A tool
granted after the turn opened does not join it. The secret globs are read
again at every dispatch and every result filter, so a glob added mid-turn
applies to the next call (one removed mid-turn applies until the turn ends);
globs that cannot be read refuse the call and withhold the result.

At the console:

```text
/runtime tools                                   show profiles (with sources), ceiling, globs, DROPPED and ERROR lines
/runtime tools allow <model|prefix*> <tool> [agent|workbench_agent]
/runtime tools revoke <model|prefix*> <tool>
/runtime tools deny <model|prefix*>
/runtime tools undeny <model|prefix*>
```

Writes happen only at the REPL with an operator present; from MCP, HTTP, a
piped console or an agent they are refused and the file is not touched. A
write takes the overlay's lock, refuses to overwrite an unreadable or invalid
overlay, validates the result and replaces the file atomically. `allow`
refuses a tool outside the ceiling or failing the check, and a model that a
tool-less profile also matches (grant on that profile's own selector, e.g.
`/runtime tools allow astra-cyber-* text_search`). Shipped entries cannot be
revoked or undenied. `runtime_policy_data()` carries the same view under
`model_tool_policy`.

Both are read again when their size or modification time changes. `/runtime
status` shows both paths and every load error.

The `tool_lane_denied_models` section of `runtime_policy.json` (`{"names":
[...], "prefixes": [...]}`) is deprecated in favour of the overlay but still read
and unioned (add-only), so existing files keep working. Neither it nor the
overlay can remove a floor entry, and no tool or MCP parameter edits either
list (`/runtime tools` at an attended console is the only writer of the overlay). A malformed section
makes the whole policy file invalid: it then loads as built-in defaults, which
also reverts every tier binding, and caller-named models are refused in tool
lanes until the file is repaired or reset (`/runtime reset`). A `local_models`
entry that names a blocked model does not invalidate the file: that one tier
uses its default, the rest of the file applies, and `/runtime status` shows a
`WARNING:` line for it. A runtime-policy update that would bind a blocked
model is refused.

A tier bound to a provider other than Ollama is judged by the model that
provider actually serves (`SONDER_INFERENCE_TIER_MODELS`, else
`SONDER_INFERENCE_MODEL`; the OpenRouter tier model; `SONDER_OPENAI_MODEL`).
A blocked model never serves a tier, in any lane: a bridged call is always a
tier call on the server lanes (a model asked for by name stays on Ollama, and
the bridged request carries no model name), so when a tier's provider is
configured to serve a
blocked model, or its reply reports one, that call is refused on every lane,
chat by tier included. The refusal names the tier, the provider, the model and
the setting that chooses it, and says to ask the model by name for a text
answer instead. On the packaged chat-service path, a single non-Ollama provider
also receives by-name and `local` requests. It serves its own configured model
for those selectors, which is judged before sending and after the reply.
Sonder Inference and OpenRouter name the served model in every
reply (the Sonder Inference gateway rejects a reply that does not), so a tier
on them with no model named is not refused up front: the model the reply
reports is judged on every call, and the lane runs when it is an ordinary one.
That includes the whole process on Sonder Inference
(`SONDER_MODEL_BACKEND=sonder-inference`) with `SONDER_INFERENCE_MODEL` unset,
the server's own agent turn on tier `sonder` included. The OpenAI-compatible
transport never reads a model name from the reply, so with
`SONDER_OPENAI_MODEL` unset (or a provider configuration that cannot be read)
the served model cannot be determined: that tier is refused in tool lanes,
with a refusal that names `SONDER_OPENAI_MODEL`, and chat still works.

A lane on the default route (tier `""`, `sonder` or `local`) is judged before
it starts by every tier that route can answer on, not only the tier it starts
on. The route starts on the policy's chat tier (on the `sonder` alias when
`SONDER_STRICT` is on or that tier is unbound) and, with
`SONDER_MODEL_ESCALATION` at its default, steps up when an answer fails or is
empty. The tiers judged are the ones the escalation plan can reach from that
start, asked of the plan's own filter (`tier_escalation.reachable_tiers` over
the configured local tiers, `runtime_policy.configured_tiers`, with no image):
the start, then every configured local tier at least as strong by role as the
start (`fast` is below `general` and `code`) and the optional `reasoning` tier;
never `vision` (the default route carries no image); with escalation off, the
start alone. The `sonder` start also judges the stable alias and its local code
fallback (`LOCAL_CODE_MODEL`) against policy-added names. So a lane that runs the reply (campaigns, game generation,
`/train`, `curriculum_run.py`, `eval_retrieval.py`, `game_ladder.py`) refuses
when a reachable tier is bound to a blocked model, is bridged to a provider
that serves one, or is on the OpenAI-compatible transport with no model named.
The refusal names the tier and the reason, which names the setting that fixes
it, or says to set `SONDER_MODEL_ESCALATION=0` to keep the route on its first
tier. This up-front check gives one clear message before any work; it is no
longer what keeps a blocked model's reply out of the lane: if a tier is bound
while the lane is running, the bridge refuses that rung's call itself. With no
such tier configured the default route escalates and the lanes run exactly as
before. These lanes read the server's in-memory policy (the one the escalation
plan uses), not the file on disk.

A lane whose prompt text it does not write itself (curriculum tasks,
retrieval-eval tasks) also judges what the prompt selects for itself: a model
it names (`use model X: ...`), an ensemble it asks (`ask code and reasoning
models: ...`), which `sonder()` would otherwise switch to, and a console
command it starts with: `/ensemble` polls the bound local tiers, so every
configured local tier is judged by name; `/vision` answers on the vision tier,
which is judged. The other commands that generate (`/work`, `/game`,
`/delegate`, `/master`) reach lanes that refuse on their own. The other
default-route lanes build every prompt from fixed text.

Generation itself is default-deny. Every model call reaches
`server._chat_request`, which refuses a denied model by name and by the
request payload's model with a `configuration` error (status 403) before
anything is sent, unless the call site passed `text_only=True`. For a bridged
tier the bridge judges the model its provider is configured to serve before
sending, and the model the reply reports after it returns, on every call
whatever `text_only` says: a blocked model is refused on every lane. An empty
reported id, `default` or an echo of the tier name names no model: refused
unless the call is text-only. A cloud fallback is judged again. Text lanes
(chat by name over MCP, HTTP and the REPL, structured chat, `ensemble_answer`,
`consult`, offloads, `model_fanout`, vision, the LLM judge, conversation
titles) declare themselves text-only and keep answering a model asked for by
name; chat by tier answers too, except on a tier whose bridged provider serves
a blocked model. The packaged gateway handed to the application is the checked
one; only the text consumers receive the unchecked gateway:
`Application.model_gateway`, whose generating consumer is the legacy bridge
above, and the chat service (A2A), which holds it through a handle that
applies the same served-model rule to every non-Ollama request it dispatches.
Ordinary models are judged against a deny list cached per version of the
policy file, so they pay no extra disk read per call.

Six configurations fail closed:

- With the shipped `model_tool_policy.json` missing, unreadable or invalid (no
  floor profile counts as invalid), every tool-using lane refuses every model,
  tier-bound ones and the default route included, and the refusal and
  `/runtime status` name the file. Text-only chat is unaffected, and tier
  bindings are not rewritten. Fix: restore the file from the install.
- With the overlay present but unreadable or invalid (or impossible to
  inspect), every tool-using lane refuses every model, tier-bound ones, the
  default route and bridged providers included, because the overlay may be
  what restricted a model and a broken file cannot say which. The refusal and
  `/runtime status` name the file, text-only chat is unaffected, tier bindings
  are not rewritten, and a runtime-policy update that binds a tier is refused
  until it is fixed. A failed read is reused for at most a second and refuses
  while it is. The file is never rewritten; fix it by hand (or delete it). A
  missing overlay is normal.
- With the policy file unreadable, a model named by the caller (not one a tier
  is bound to) is refused in tool lanes, and the chat code gate does not run
  its reply. Fix: repair the file (`/runtime status` shows the error) or reset
  it with `/runtime reset`.
- An OpenAI-compatible bridged tier with `SONDER_OPENAI_MODEL` unset sends the
  tier name as the model and reports the id it sent, which names no model, so
  every tool lane on that tier is refused (and, when the default route can
  reach that tier, every lane on the default route). Fix: set
  `SONDER_OPENAI_MODEL` to the model the endpoint serves.
- A tier whose bridged provider serves a blocked model no longer answers chat
  by tier (MCP, HTTP, A2A, ensemble, consult). Fix: ask the model by name for
  a text answer, or change the model that provider serves for the tier.

A denied model's text is not barred from tool lanes as input: lesson
candidates distilled from its answers, the agent `offload` tool's answer and
any text a person pastes are prompt input that another, permitted model (or a
person) chooses to act on. What the deny list guarantees is that the denied
model's own reply is never run, applied or turned into a tool call by the
runtime.

The lanes that generate through a text entry point and then run, compile or
write the reply refuse first: `/train` practice (HTTP and REPL),
`curriculum_run.py`, `eval_retrieval.py`, `eval_harness.py` and `eval_solver.py`
live runs, `promotion_eval.py`, `game_ladder.py` and the arena-shooter
example drivers. `/run`, `/runproject` and the REPL `/refactor` (applied only
after the user confirms the diff) still act on what any model wrote: the user
typing the command is the explicit approval.

Limits: this is a name list. A renamed copy (`ollama cp astra-cyber-27b
helper`) or an alias such as `sonder:latest` pointed at a blocked model is not
recognised. A provider that maps the configured id to a different model while
reporting a permitted one is not detected. The OpenAI-compatible gateway never
reads the `model` field of the reply (its `ModelResponse.model` is always the
id it sent), so for that provider the check after the reply adds nothing to the
check before sending: the configured `SONDER_OPENAI_MODEL` is what is judged.
Because that model cannot be identified at the bridge, an OpenAI-compatible
tier with no model named that is bound while a default-route lane is running
answers that lane's text call like any undetermined tier; only the lane's
up-front check refuses it, and only for tiers configured when it ran.
If the policy file is unreadable, built-in names are still enforced everywhere;
names added only in that file are enforced for caller-named models, but not for
tier-served ones until the file is readable again. `/runtime status` reports the
unreadable file. A name added while a job runs is enforced for that job's
by-name text calls when the job next starts; tiers are covered immediately. A
streamed HTTP chat turn forwards a bridged reply's tokens to the client as they
arrive. On Sonder Inference, streamed chat by tier with no model named can
display text from a denied model before the reported model is refused; that
display-only text is not stored or run. Tool-lane generation runs with the
stream disarmed. Text-lane output applied by
an external program (an MCP or HTTP client that splices `ensemble_answer`
output, or executes tool calls returned by `/v1/chat/completions`) is outside
the runtime's control.

##### Tool-lane deny list: classifying a generation call site

`tests/test_generation_call_sites.py` fails on any new call site of the
generation stack (`_post_model`, `_chat_request`,
`_chat_request_with_cloud_fallback`, `_make_generate`, `_make_tier_generate`,
`_generate_text`, `_gateway_generate_text`), of a text entry point (every
wrapper over a stack that declares `text_only=True`: `sonder`, `_sonder_impl`,
`_sonder_impl_serialized`, `_answer`, `answer_with_history`,
`_answer_with_history_impl`, `structured_answer_with_history`,
`ensemble_answer`, `consult`, `offload`, `_offload_impl`, `_offload_tier_impl`,
`model_fanout` and its run, resume and synthesis helpers, `vision_analyze`,
`_vision_analyze_impl`, `_internal_generate_for_route`), of a raw model
endpoint path, of a packaged gateway `.generate` (two or more positional
arguments or a `request=`/`context=` keyword, anywhere), or of the unchecked
gateway (`raw_gateway`, or any read of `.model_gateway`). A call through an
imported alias, a name bound to one of these or a `functools.partial` counts as
a call of the original. Register the site in
`tests/support/generation_call_sites.py` with its count, class and reason:

- `TEXT`: the reply is only shown, parsed or stored as text. Pass
  `text_only=True` at a generation-stack call; a text entry point already
  declares itself.
- `EXEC`: the reply drives tools, or is run, compiled or written. Leave
  `text_only` at its default. A caller of a text entry point or a raw endpoint
  must refuse first (`tool_lane_guard.selector_refusal`, `resolved_refusal`, or
  `denied` with `refusal`), in the same function or in every guard listed in
  the entry's `guarded_by` (each guard must refuse and judge on its own). The
  refusal counts only when its result stops the function: it is a direct
  statement of the function body, before the statement holding the call, not
  nested in an `if`, `try`, `with` or loop (a loop body counts for a call in
  the same loop body), and it is either an `if` whose test holds the refusal
  and whose body ends in `return` or `raise` (`continue` or `break` in a loop
  body), or a call statement of a same-file helper that refuses that way with
  `raise`. It judges the selector the call passes when it judges the same
  expression and nothing in it is changed between the refusal and the call: no
  name in it is assigned, and no `setattr` or method call is made on it (a
  read-only method call counts too, so keep such calls before the refusal; a
  call that passes none is judged by `""`, the default route). The check
  recognises these fixed shapes and is meant to catch an honest omission, not
  code written to get around it. Where a
  different expression is right by design, the entry says why in
  `selector_reason`.
- `CHOKE`: the generation stack forwarding the flag, imports and re-exports.
- `ACCEPTED`: a named exception with its reason.

The scanner recognises a fixed set of refusal shapes. Write a new EXEC
consumer in one of them: a direct `if` test of a refusal call, a name or
walrus bound to it, or one of those as the first operand of `or`, with a
stopping body. `selector_refusal` must receive `TIERS`, `server.TIERS`, or a
name read from an enclosing runtime function's `TIERS` with an empty fallback
(the `known or {}` form in `mesh_agent_tools.py`). It cannot prove statically
that `rt` is the runtime; review must verify that binding. Other shapes are
not credited.

`GAP` (runs model output with no refusal) is not accepted: the list of
tolerated gaps is empty. Limit: a name computed at run time
(`getattr(server, "ens" + "emble_answer")`, or `getattr(server, name)` with a
variable) cannot be seen statically, so the ratchet does not catch it; review
has to.

#### Reference lookup

For some local models a chat turn first searches a local reference library
(an index built outside Sonder: security references such as Windows event
pages, MITRE ATT&CK and Sigma rules, and, in a version 2 index, coding and
debugging references; see "Setting up the reference library" below) and sends the passages it found inside the user turn, between marker
lines that carry a random code for that turn. The built-in models are the
same names as the deny list above, taken from its default prefixes: the
`astra-cyber-` prefix and the two Hugging Face GGUF sources (as `hf.co/...` or
`huggingface.co/...`) in any quantisation, tagged or not. The model is first asked for search terms
(temperature 0, 80 tokens, no system prompt, never streamed, not a provider
attempt of the turn; with model thinking off where the model's route accepts
that, and asked once more as an ordinary call when that reply comes back empty
or refused); only the first eight identifiers in a question (event
ids, technique ids, hex values) get title priority; "T1110" there finds the
parent technique's page, not its sub-techniques'. A chosen passage whose title
opens with an ATT&CK id (an emulation test or detection rule titled
"T1003.001 ...") is followed by the passage of every other source whose title
opens with the same id, so the ATT&CK page sits next to the pages that quote
it (one title query per such id). Passages are handed to the
model only when the best of them is close enough to the question: it must hold
at least a fifth of the query's search words, each word weighted by how
rare it is in the library (terms without a space only: a multi-word phrase the model
wrote counts through its words, not as a phrase). A question the library does not cover (a long
design question about another subject shares many common words with it) is
answered without passages and says
`Sources: none (no reference passage was close enough to the question)`. The
same search-terms call is also asked, on a second line, whether the question
is in the library's subjects at all (for a version 1 index: "about computers,
networks, accounts, logs or security in any way"; for a version 2 index: "about
any of these subjects", the subjects the index names, which the request also
lists); a reply of
`UNRELATED` raises the floor for that turn from a fifth to two fifths. The
verdict never removes passages by itself (the smaller measured model gave it
to five of 44 covered questions), and a reply without a verdict leaves the
floor where it was. Short questions on other subjects that the model judges related, or
whose best passage reaches two fifths, still get passages. The
floors are constants measured on the security-only library and not yet
re-measured on the expanded one
(`sonder_runtime/domain/reference_lookup/relevance.py`); they do not apply to
the `reference_search` tool, which returns the nearest passages as they are.
The reply is cleaned
(copied marker lines removed, display-safe text), its `[n]` citations are
checked, and a `Sources:` block with any warnings follows it. When the answer
cites none of its passages the block starts
`Sources: retrieved but not cited in the answer`, so passages the model was
given but did not use are not read as the answer's support. The stored task
is the raw question and the stored reply the cleaned text; the Sources block
is added after storage and capture, and a turn answered with passages is
excluded from lesson distillation and the training export.

| Control | Effect |
|---|---|
| `SONDER_REFERENCE_INDEX` | path of the index file; default `references/references.sqlite` under the Sonder home. Opened read-only through the runtime's SQLite factory: on POSIX that removes group/other permission bits from the file when the runtime's user owns it, and a managed child process only admits an index inside its private roots (elsewhere the lookup reports itself unavailable) |
| `SONDER_REFERENCE_LOOKUP=0` | kill switch: no automatic lookup for any model, and `reference_search` answers unavailable |
| `reference_lookup` in `runtime_policy.json` | `{"enabled": true, "names": [...], "prefixes": [...]}`: entries add to the built-in names, never remove them; `enabled: false` turns automatic lookup off; a `path` key is rejected, because the index path comes only from the environment |

The index itself is built by a separate tool and is not shipped with Sonder
(see below). A missing or unusable index fails open: the turn answers without
passages and says `Sources: none (reference lookup unavailable: <reason>)`.
The runtime policy status output and `diagnostics` show one line with the
index path, its state, the schema version, the passage sources with their
counts, the names in force and the last error. A cancelled search-terms call
ends the turn in the chat routes. It does not in `consult`: `consult` turns
any exception from a member call into that member's error entry (as it did
before this feature), so a cancellation there is reported as that member's
error and the call goes on.

##### Setting up the reference library

The reference library is a read-only search index of public documentation:
security references (Windows event pages, MITRE ATT&CK, Sigma rules) and, in a
newer index, coding and debugging references. Sonder does not ship it and does
not build it. Local models that the lookup rule names (the `astra-cyber-` models
and the Hugging Face GGUF sources by default, plus anything the policy's
`reference_lookup` section adds) search it automatically before they answer, and
any MCP client can search it with the read-only `reference_search` tool. When a
turn used passages, the reply ends with a `Sources:` block that lists them, and
`Sources: none (<reason>)` says why none were used (no passage was close enough,
or the library is unavailable). Nothing is fetched at question time.

1. Get the builder, a separate repository, `Krilliac/sonder-references`. It
needs Python 3.12 or newer and no packages outside the standard library.

```
git clone https://github.com/Krilliac/sonder-references.git
cd sonder-references
```

2. Fetch the sources. `--all` fetches all of them; `--list` prints their names
and licences, and you can name a subset instead (for example `attack sigma
cwe`). `--verify` then reports any file that is missing, for example one an
antivirus removed. On Linux or macOS use `python3` in place of `py -3.12`.

```
py -3.12 -I fetch_references.py --all
py -3.12 -I fetch_references.py --verify
```

3. Build the index, either straight into the default location below or into a
file of your choice (`<index path>`) that you point Sonder at.

```
py -3.12 -I sentinel.py index --out "<index path>"
```

That repository's README covers updating, antivirus false positives, the
licences of each source and troubleshooting.

Connect the index to Sonder in one of two ways.

- Build it into the default path, `<Sonder home>/references/references.sqlite`.
  The Sonder home is, on Windows, `%LOCALAPPDATA%\sonder`
  (for example `C:\Users\<you>\AppData\Local\sonder`); on Linux
  `$XDG_DATA_HOME/sonder`, or `~/.local/share/sonder` when that is unset; on
  macOS `~/Library/Application Support/sonder` (an existing
  `~/.local/share/sonder` is kept when the macOS folder does not exist yet).
  Three things override all three, strongest first: a home set inside the
  process (the app's own `configure_home`), then `SONDER_STATE_HOME`, then
  `SONDER_HOME`.
  Create the `references` folder if it is not there.
- Or set the path of the file in the environment of every Sonder process, for
  example for PowerShell:

```
$env:SONDER_REFERENCE_INDEX = "D:\data\references.sqlite"
```

and for a POSIX shell:

```
export SONDER_REFERENCE_INDEX=/data/references.sqlite
```

The file is checked again whenever its modification time or size changes, so
a rebuild is picked up by a running Sonder without a restart. Build to a new
file and rename it over the old one when you can, so a search never meets a
half-written index.

Check that it worked. The policy status output and `diagnostics` show the
lookup line. A working version 2 index looks like this (names and counts are
the ones in your index):

```
index=/home/<you>/.local/share/sonder/references/references.sqlite readable=yes schema=2 passages=8908 sources=attack:640, events:3400, sigma:4868 built=2026-11-13T09:00:00+00:00; automatic lookup names=...
```

`readable=yes` means the index passed validation; `schema=` is its version (1
for an index without a `schema_version` row). Then run one search from any MCP
client (a question your library covers, here a Windows event id):

```
reference_search(query="event 4625 failed logon", k=3)
```

A result with framed passages and a `Sources:` list means the whole path works;
`reference lookup unavailable: <reason>` names what is wrong.

To turn the lookup off for a process, set the kill switch. Automatic lookup
stops for every model and `reference_search` answers unavailable:

```
$env:SONDER_REFERENCE_LOOKUP = "0"
```

Troubleshooting:

- **`readable=no (missing: file not found)`.** The file is not where Sonder
  looks. The status line starts with the path it used; compare it with where
  the builder wrote the file, and check whether `SONDER_REFERENCE_INDEX` is set
  in this process (an empty value means the default path is used).
- **Schema too new.** The status line shows `readable=yes` with a warning
  `index built by a newer library tool (schema N, this runtime reads up to 2)`.
  The index is still searched, because its tables passed validation, but its
  list of subjects is not used: the search-terms request falls back to the
  security-only wording. Update Sonder, or rebuild the index with the matching
  builder version, to get the subjects.
- **Warnings about `sources` entries.** A malformed or oversized entry in the
  index's metadata is ignored and logged as a warning on the
  `sonder.reference_lookup` logger; the lookup keeps working with the rest.
- **Answers have no `Sources:` passages.** Read the reason in the line itself.
  `Sources: none (no reference passage was close enough to the question)` is
  the relevance floor: the question is outside the library or shares too few
  words with it. `Sources: none (reference lookup unavailable: ...)` is an
  index problem, so read the status line. If the model is not named by the rule
  (`names=` and `prefixes=` in the status line), no automatic lookup runs at
  all; add it under `reference_lookup` in `runtime_policy.json`.
- **A UNC path (`\\server\share\references.sqlite`) is refused or unreadable.**
  A managed child process only admits an index inside its own private roots;
  elsewhere the status line reads `readable=no (error: index not admitted by the
  runtime: ...)`. SQLite also locks unreliably over network shares. Copy the file to
  a local folder and point `SONDER_REFERENCE_INDEX` at the copy.
- **It works in one process but not another.** Each process reads
  `SONDER_REFERENCE_INDEX` from its own environment, fixed when it started. The
  MCP server (`python -m sonder_runtime mcp`) is long-running: one started
  before you set the variable (or before you changed it) still looks at the old
  path, so it reports the file as missing while a new shell finds it. A rebuild
  alone does not need a restart; a changed variable does. Restart the MCP
  server, and the app or console that launched it, from a shell that has the
  variable set, then read the status line again.

Lookup applies to the MCP chat tool (by tier or `model_override`), the HTTP
chat route, text-mode `ensemble_answer` (one retrieval shared by every member
when any member matches; the synthesiser sees only the cleaned answers) and
`consult` (members share one retrieval; the judge sees no passages). It does
not apply to structured answers, code-mode ensembles or model fan-out. A tier
bound to a provider other than Ollama is judged by the model that provider
serves, as for the deny list. A deny-listed model never serves a tier (that
call is refused), so on a bridged tier lookup applies only to a name the
policy's `reference_lookup` section adds; a refused call leaves no Sources
block, no stored interaction and no lookup state. When a provider falls back to another backend
in the middle of a call, the lookup decision follows the primary provider's
configured model. The read-only MCP tool `reference_search`
(`query`, `k` 1 to 8) returns framed passages and their sources to any caller;
it makes no model call.

Every model call the lookup makes is a text call under the deny list's
generation check: the search-terms call declares `text_only=True` itself, and
the framed answer, each ensemble or consult member, the synthesis and the
consult judge carry the declaration of the lane they belong to. Lookup runs
only on a lane that declares itself text-only. A model both rules name
therefore answers with lookup on these lanes when asked for by name, while
the lanes that run or apply model output still refuse it and make no lookup
call.

A lookup turn is sent none of the runtime's system prompt (persona, playbook,
trace instructions, tool guidance, profile and goal), so the model's own
Modelfile rules stay in force; only a system message the caller supplied to
the MCP chat tool is sent. The HTTP receipt says so:
`sonder_receipt.reference_lookup` holds `applied`, `passages` or `reason`
(`low_relevance` when the floor removed the passages), and
`runtime_system_prompt_sent: false`. The status line says so too.

One exception to "passages are never stored": when a turn's provider attempts
are captured in the durable session event stream, the `provider.requested`
event keeps the payload that was sent, which is the framed prompt with its
passages, and a session export (including ATIF) can carry it. The passages are
public reference text and the record is transport evidence; it is not fed
back to a model.

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
| `SONDER_INFERENCE_PRIVATE_WORKERS` | unset | JSON list of approved private workers (https private IP, own CA bundle, own token variable); needs `SONDER_ALLOW_REMOTE_INFERENCE=1`, not cloud consent; requests are placed on the least-loaded endpoint ([provider reference](../architecture/sonder-inference-provider.md#private-workers)) |
| `SONDER_INFERENCE_TIMEOUT_SECONDS` | `300` | per-call ceiling, never beyond the operation deadline |
| `SONDER_INFERENCE_STREAM_IDLE_TIMEOUT_SECONDS` | `0` (off) | Streaming only: abort when no bytes (keep-alive comments count) arrive for this long once the response has started; `0`-`86400`. Inside the total timeout above. Off by default because a long prefill can legitimately be silent. The error names the idle timeout and the elapsed seconds. |
| `SONDER_INFERENCE_STREAM_FIRST_CONTENT_TIMEOUT_SECONDS` | `0` (off) | Streaming only: abort when no content, reasoning or tool-call delta has arrived this long after the request started (empty keep-alive deltas do not count); `0`-`86400`. Pair a long `SONDER_INFERENCE_TIMEOUT_SECONDS` with these for a slow large model that must still fail fast when it stalls. The `openai_compatible` provider reads `SONDER_OPENAI_STREAM_IDLE_TIMEOUT_SECONDS` and `SONDER_OPENAI_STREAM_FIRST_CONTENT_TIMEOUT_SECONDS` (same meaning); it streams only when the caller streams. The idle clock resets per complete line received, so one line trickling in for longer than the limit counts as idle. If the endpoint answers a stream request with a single JSON body, the idle limit bounds the whole body read after the headers, and the first-content limit then acts as a cap on time-to-headers. Streamed turns on `openai_compatible` tiers now send `"stream": true` and `stream_options: {"include_usage": true}`; an endpoint that rejects either field fails with "endpoint rejected request (HTTP 400)". |
| `SONDER_INFERENCE_HEALTH_TTL_SECONDS` | `5` | health-cache lifetime |
| `SONDER_INFERENCE_FALLBACK` | `none` | `ollama` sends requests Inference never received to local Ollama once, and serves schema/tool/image chat steps (which Inference v1 cannot carry) on local Ollama |

Provider bindings apply to every model call that selects a tier. This includes
interactive agents and workbench turns, autopilot planning, task execution,
validation, adaptive replanning and end reports, plus
`master_orchestrate`/fleet workers and audit/helper calls. The provider is
resolved from the tier binding for that call; it is never inferred from the
model name. A `sonder-inference` binding therefore sends the request to
`SONDER_INFERENCE_BASE_URL` and cannot silently enter the Ollama pool.

The supported provider values are `sonder-inference`, `openai_compatible`,
`ollama`, `openrouter`, and `anthropic`. Loopback OpenAI-compatible endpoints remain local; remote
endpoints require the operation's cloud consent and the provider's transport
policy. OpenRouter requires `SONDER_ALLOW_CLOUD=1`, an API key, and the
operation's cloud consent. Anthropic requires the same, with
`SONDER_ANTHROPIC_API_KEY` (optional `SONDER_ANTHROPIC_API_KEY_2`), the optional
`anthropic` SDK, and a local daily cap `SONDER_ANTHROPIC_DAILY_USD` (default 10);
see `docs/integration/anthropic.md`. Autopilot
and other local-only routes refuse a hosted binding rather than silently
changing providers. Ollama-bound tiers retain the existing request payload
and route behavior byte-for-byte.

Inference fallback is fail-closed by default. Set
`SONDER_INFERENCE_FALLBACK=ollama` only when a fallback is explicitly wanted;
Ollama is then permitted once, and only after the primary provider proves that
it did not execute the request. Timeouts, partial responses, and an unknown
execution outcome do not qualify for fallback. The same declaration lets a
chat step that needs an Ollama-only feature (a decoder schema from
`offload(schema=...)`, `extract_grounded` or agent JSON steps, native tools,
images) run on the loopback Ollama daemon with the tier's policy model instead
of being refused; a `-cloud` model or remote Ollama endpoint is still refused,
and the turn receipt records `served by ollama (ollama-only feature: ...)`.
Without the declaration such a step fails with a 400 naming
`SONDER_INFERENCE_FALLBACK=ollama` as the fix.

For local agent, autopilot and fleet work, pool admission waits in a bounded
queue. `SONDER_POOL_ADMISSION_TIMEOUT_SECONDS` controls that wait (default
`30`, valid range `0..60`) and is capped by the remaining model-call
deadline. A value of `0` disables the additional queue wait. The queue still
requires every configured origin to be loopback or literal private/link-local LAN; remote Ollama
consent and HTTPS rules are unchanged. Interactive chat and ordinary offload
keep the existing one-second fail-fast admission behavior.

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
ceilings below.

Automatic sizing is also corrected by measurement. With exactly one Ollama
origin configured, Sonder reads `/api/ps` (at most once a minute per model,
never on a model's first request) and compares `size` with `size_vram`. A
model split between VRAM and system RAM gets a smaller automatic window,
computed from the measured overflow and the model's attention geometry, for
30 minutes; the `observed-spill` clamp records it. Fully GPU- or CPU-resident
models are never clamped, an explicit pin is never overridden, and
`SONDER_RESIDENCY_FEEDBACK=0` disables the probe. If a spill is larger than
the whole KV cache, the weights exceed VRAM and Sonder logs that a smaller
model or quantization is needed instead.

An explicit pin is a process-wide default, not scoped to one
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
`SONDER_UPDATE_ALLOW_UNSIGNED` (dev only), `SONDER_UPDATE_TRUSTED_ROOT` (path to
the operator-installed TUF root; default `<SONDER_HOME>/updates/trusted_root.json`),
`SONDER_BRANCH_PREDICTOR`.

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

## Editing system and agent prompts

Every system or agent prompt that Sonder sends to a model is a Markdown file:
personas, the agent and research system prompts, the Autopilot and router
prompts, the lesson distillers, the model-identity block and the rest. The
shipped defaults are in [`prompts/`](../../prompts/README.md) in the
repository. To change one, don't edit the repository copy. Put a file with
the same relative name in the override directory:

- `SONDER_PROMPTS_DIR`, when set, is searched first.
- `<Sonder state home>/prompts/` (for example
  `%LOCALAPPDATA%\sonder\prompts\personas\coder.md`) is searched next.

Overrides are read again on the next turn, the same way `system_profile.md`
is, so they take effect without a restart or a code change. Within one turn
the text doesn't change. The console command `/prompts` is read-only:

| Command | Shows |
|---|---|
| `/prompts` or `/prompts list` | Every prompt, with `default` or `override@<hash>` |
| `/prompts show <name>` | The text in effect and its placeholders |
| `/prompts path <name>` | Where an override for that prompt goes |
| `/prompts reload` | Clears the read cache |

On the HTTP surface, `/prompts` requires developer authority.

Some prompts are templates with `$name` placeholders. An override must keep
exactly the same set of placeholders, and `$$` produces a literal `$`. Sonder
ignores an override and uses the shipped default, with one logged warning, if
the override is empty, larger than 64 KiB, not UTF-8, unreadable, resolves
outside its directory, or has the wrong placeholders. A bad edit never breaks
a chat. Each chat turn's trace records which prompt versions it used; see
`turn_inspect`.

`ollama_alias_system.md` is baked into the `sonder` Ollama alias, so a change
to it applies only when the alias is rebuilt. For the full list of prompts
and the rules for each, see [`prompts/README.md`](../../prompts/README.md).
