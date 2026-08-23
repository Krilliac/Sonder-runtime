---
name: sonder-agents-and-fleets
description: >-
  Operate Sonder's bounded autonomy stack - autopilot runs, master orchestrator
  fleets, model fanout, and the multi-PC Ollama worker pool. TRIGGER when the
  user says "autopilot", "fleet", "master_orchestrate", "cancel the agents",
  "model fanout", "worker pool", "agent stuck", "use N workers", or asks how
  cancellation, retries, failover, or fleet provenance work. DO NOT TRIGGER for
  self-modification runs (selfmod propose/verify/promote) - that is
  sonder-selfmod-lifecycle; permission modes, redaction, and trust boundaries
  live in sonder-security-and-privacy.
---

# Sonder Runtime: agents, autopilot, fleets, and inference pooling

Sonder ships three layers of increasing autonomy, all built on the same rule:
**the host, never the model, decides which states and actions are legal and
whether evidence satisfies a gate** (`autopilot_controller.py:1-8`). Everything
below was verified against real source at commit `99162cf9`.

| Layer | What it is | Durable store | Entry points |
|---|---|---|---|
| Agent loop | One model driving guarded JSON tool calls | none (per request) | `workbench_agent`, chat |
| Autopilot | Restart-safe autonomous goal run with plan + owner heartbeats | `autopilot.db` via `sonder_runtime/adapters/persistence/autopilot_store.py` | `autopilot_start/resume/pause/cancel/status` (server.py:21407-21442), REPL `/autopilot ...` |
| Fleet | Parallel delegated subagents under a master | `fleet.db` via `sonder_runtime/adapters/persistence/fleet_store.py` | `master_orchestrate` (server.py:8425), `master_status/capacity/cancel/retry`, REPL `/master ...`, `/agents` |

Model fanout ("ask every model the same question") and the multi-PC Ollama
worker pool are inference-side siblings of the fleet layer; both are covered
below.

## Autopilot: durable bounded goal runs

The controller (`autopilot_controller.py`) is request/thread scoped and owns
**no durable resources**; `autopilot_store` owns persistence and cross-process
control. Hard bounds, all compiled constants (`autopilot_controller.py:19-31`):

| Bound | Value |
|---|---|
| Task kinds | `inspect`, `research`, `implement`, `validate`, `report` |
| Policies | `observe`, `workspace` |
| Tiers | local only (`LOCAL_TIERS`); cloud tiers are rejected at `normalize_tier` (autopilot_controller.py:68-74) |
| `MAX_TOTAL_CYCLES` | 50 |
| `MAX_ADAPTIVE_CHECKPOINTS` | 6 |
| `MAX_TASK_OUTPUT` | 32000 chars |
| Failure prefixes | `ERROR:`, `VALIDATION_FAILED:`, `EVIDENCE_REQUIRED`, `CANCELLED` |

Start / control (MCP tools; REPL equivalent is `/autopilot status|resume|cancel`):

```
autopilot_start(objective, project="", tier="auto", policy="workspace",
                allow_web=True, max_cycles=12, max_failures=3, max_tasks=12,
                max_replans=2, adaptive=True, plan_only=False, wait=False)
autopilot_resume(run_id, max_cycles=12, wait=False)   # paused/blocked/ready/interrupted
autopilot_pause(run_id)     # cooperative, next host checkpoint
autopilot_cancel(run_id)    # active task result is discarded
autopilot_status(run_id="", include_finished=True)
```

Lifecycle (docs/wiki/07-agent-autopilot-fleet.md): `ready/planning -> running ->
paused | blocked | completed | failed | interrupted | cancelled`. `interrupted
-> running` and `failed -> running` require an **explicit** resume/retry;
`completed`/`cancelled` are terminal and never replay.

### Interruption recovery (crash, drain, kill)

Runbook: `docs/runbooks/autopilot-interruption.md`. Every run records an owner
process with heartbeats; an unclean stop leaves an explicit `interrupted`
state, never a silent replay.

1. Check state: `GET /v1/sonder/status` or REPL `/autopilot status`.
2. Review what the run was doing (mid-write workspace changes first).
3. `/autopilot resume <id>` or `/autopilot cancel <id>`. Resume re-claims
   ownership with a fresh heartbeat; a stale owner cannot overwrite the new one.

A task that was `running` when the controller died becomes **`uncertain`**, not
re-queued: the host cannot prove whether the crash happened before, during, or
after a mutating tool call, so automatic replay is refused and the task is
preserved as operator evidence (`_mark_interrupted_tasks_uncertain`,
autopilot_controller.py:267-285).

Invariants (violations are bugs - file them): unknown liveness never causes two
owners; terminal tasks never replay; budgets hold even when planners fail.

## Master orchestrator: fleets

`master_orchestrate(task, mode, agents, worker_cap, tier, learn, retry_of,
project)` (server.py:8425). Modes:

| Mode | Behavior |
|---|---|
| `ask` (default) | Returns the choice prompt with computed capacity - use for sizing |
| `inline` | Master handles the task directly, no subagents |
| `delegate` | Queue bounded subagents, audit, merge, then return |
| `fleet` | Queue full breadth in the background, return immediately; monitor via `master_status()` |

Task phrases like "fleet", "swarm", "parallel agents", or "use 24 workers"
auto-select fleet mode; `requested_worker_cap` parses exactly one affirmative,
unquoted, non-negated "use N workers" directive (master_orchestrator.py:471-488).

**Project confinement**: for repository work pass `project` as an existing
canonical root. Every child and the aggregate are confined to it; missing or
conflicting repository scope **fails closed** instead of inheriting the cwd
(server.py:8445-8447, 8530-8534).

### Capacity model (master_orchestrator.py:52-79, 491-597)

| Constant | Value |
|---|---|
| `DEFAULT_MAX_AGENTS` / `ABSOLUTE_MAX_AGENTS` | 16 / 64 |
| `DEFAULT_MAX_WORKERS` / `STANDARD_MAX_WORKERS` / `ABSOLUTE_MAX_WORKERS` | 8 / 16 / 64 |
| `RAM_RESERVE_BYTES` + per worker | 1.5 GiB reserve + 0.25 GiB/worker |
| `GPU_RESERVE_BYTES` + KV per worker | 0.5 GiB reserve + 0.5 GiB KV cache/worker |
| `HEARTBEAT_SECONDS` | 5 |

Worker slots = min over: requested agents, `logical_cpus // 2` (capped 8), RAM
headroom / 0.25 GiB, GPU VRAM headroom / 0.5 GiB KV, and Ollama's own
`OLLAMA_NUM_PARALLEL` batch width - handing Ollama more concurrency than it
batches just queues requests (master_orchestrator.py:508-533). A "worker" is a
Python thread POSTing to Ollama; model weights load **once** in the Ollama
process, so extra workers cost a KV cache, not another model copy
(master_orchestrator.py:64-76).

Overrides, from weakest to strongest clamp:

- `SONDER_PARALLEL_WORKERS` env: clamped to `STANDARD_MAX_WORKERS` (16) and the
  operator ceiling (master_orchestrator.py:538-546).
- Per-run `worker_cap` (or "use N workers"): may exceed 16 but never the
  operator ceiling or compiled 64 (master_orchestrator.py:547-555).
- `SONDER_MAX_WORKER_CAP` env: operator ceiling; malformed values fall back to
  16, values above 64 clamp to 64 (master_orchestrator.py:428-434).

Check before launching: `master_capacity(requested_agents, worker_cap)` shows
`worker_slots`, `bound_by`, and warnings - including the important one that an
unset `OLLAMA_NUM_PARALLEL` can collapse to 1 on a VRAM-tight box and serialize
everything (master_orchestrator.py:559-572).

### Reload-safety split

Process-local execution state survives `importlib.reload` via `if "_LOCK" not
in globals()` guards (master_orchestrator.py:26-50). The durable ledger lives
in `fleet_store`, which is deliberately **not** in `LIVE_RELOAD_MODULES`
(server.py:908-992 lists `master_orchestrator` and `fleet_provenance` but not
`fleet_store`), so hot-reloading the orchestrator never orphans the ledger.

## Cancellation semantics ("cancel the agents")

```
master_cancel(agent_id)      # exact ID, unique prefix, or 'all' (server.py:8714-8733)
autopilot_cancel(run_id)
model_fanout_cancel(run_id)
```

- Cancellation is **cooperative**: `request_cancel(selector)` marks rows
  (master_orchestrator.py:1002-1008); the flag is checked at
  `_begin_model_call` before each model call (master_orchestrator.py:991-999).
  An active Ollama/HTTP call cannot be force-killed - the late result is
  discarded, and `master_cancel` output reports "active model calls awaiting
  return" for exactly this reason.
- Terminal marker outputs are `ABORT_MARKERS = ("CANCELLED", "INTERRUPTED")`
  (master_orchestrator.py:79).
- Children inherit cancellation at INSERT: `create_agent` reads the parent's
  `cancel_requested` inside the same write transaction, and a child of a
  cancelled parent is born cancelled with summary "cancelled before model call"
  (fleet_store.py:601-626). You cannot race a cancel by spawning faster.
- In-model-call counting uses the full-table aggregate, not the paginated
  agent list - beyond ~65 active rows a per-page sum would read 0 and callers
  would evict the model mid-generation (master_orchestrator.py:1017-1033). If
  the ledger is unreadable the count fails **closed** to 1.

## Fleet store: the durable ledger

`sonder_runtime/adapters/persistence/fleet_store.py` (root `fleet_store.py` is
a compatibility shim). SQLite, restart-safe, principal-authenticated.

- **Principals**: every write can carry `principal_id`/`principal_secret`;
  secrets are hash-verified (`_authenticate_principal`, fleet_store.py:199-216).
  A child's principal must match its parent tree and its project must match the
  parent's, or the insert raises `PermissionError` (fleet_store.py:604-611).
- **Idempotency**: primary-key IDs, `retry_of`/`retried_by` columns
  (fleet_store.py:104-105), and immutable `master_task_digest` /
  `delegated_task_digest` SHA-256 fields (fleet_store.py:652-653). Retrying a
  control request returns the existing operation instead of a duplicate;
  `master_retry` only accepts `interrupted|failed|task_drift|cancelled` rows
  (server.py:8737-8747).
- **Messaging**: `queue_agent_message` supports two modes
  (fleet_store.py:53,1080-1109): `steer` (cooperative instruction at the
  recipient's next safe checkpoint; requires a queued/running recipient) and
  `follow_up` (durable, deliverable later). Pending TTL is clamped to
  60 s .. 7 d (fleet_store.py:1109). `claim_agent_messages` is an **atomic
  claim with explicit delivery receipts** - delivery means claimed by the host,
  not accepted by the model (fleet_store.py:1185-1250); expired and
  stale-steer messages are swept in the same transaction.
- **Test isolation**: the pytest suite calls `fleet_store.clear_all()` before
  each test (tests/conftest.py:49-53) against a test-scoped database, so the
  live restart-safe ledger is never touched by test runs.

Fleet agent statuses: `queued -> running -> done | failed | cancelled |
interrupted`; `interrupted`/`failed`/`cancelled` are re-dispatchable to
`queued` via explicit retry (docs/wiki/07-agent-autopilot-fleet.md).

## Fleet provenance: protected objective runs

Purpose: a protected fleet task must **prove it looked at the target**; a
negative claim ("no implementation exists") without host-observed evidence is
rejected. All logic in `fleet_provenance.py`.

Opt in with a standalone marker line in the task:

```
[objective:history-eval|file:eval_history.py|symbol:main]
```

Grammar `OBJECTIVE_MARKER` (fleet_provenance.py:21-25): id up to 64 chars, file
a bounded public repo-relative POSIX path (no absolute paths, no `..`, no
backslashes - `_public_path`, fleet_provenance.py:86-111), symbol up to 128
chars. Sensitive names (`.git`, `.ssh`, `.env*`, `credentials.json`,
`secrets.json`, `*.key`, `*.pem`, ...) are never valid objectives
(fleet_provenance.py:37-44). Bounds: `MAX_OBJECTIVES=32`,
`MAX_TARGET_BYTES` 2 MiB, `MAX_TASK_CHARS` 32000 (fleet_provenance.py:15-20).

Enforcement pipeline:

1. `validate_delegation` (fleet_provenance.py:259-312) - before any model
   call: master/delegated task digests must match, the objective contract must
   appear verbatim in the delegated task, and the host verifies each target
   file contains the symbol. Any mismatch sets `task_drift`.
2. `validate_result` (fleet_provenance.py:402) - a worker result is accepted
   only with an `=== TOOL EVIDENCE ===` block whose evidence steps come from
   `file_read`, `file_read_range`, `text_search`, or `script_search`
   (fleet_provenance.py:48-50). `NEGATIVE_CLAIM` (fleet_provenance.py:26-32)
   flags "no implementation exists"-style text; without target evidence it is a
   false negative. Three-plus identical evidence blocks count as a repeated
   tool loop.
3. `validate_aggregate_output` (fleet_provenance.py:480) - the same checks
   again before aggregation; the host also rejects a result if the target file
   changed during the model call.

Protected runs require local worker and audit tiers including retries, so task
text and repository evidence never route to a hosted model. `task_drift`
suppresses the drifted output and keeps it out of the learning path. Runs
without markers keep ordinary fleet behavior.

## Model fanout

Six `model_fanout_*` tools (server.py:24003-24190; profile map at
server.py:22630-22649): `model_fanout`, `_status`, `_recent`, `_cancel`,
`_resume`, `_synthesize`. REPL: an imperative whole turn such as
`ask all available local models: summarize this design`; recover with
`/fanouts` after a restart.

Rules (server.py:24003-24026 and docs/wiki/07):

- **Local models run serially** to avoid GPU/VRAM contention.
- Cloud requires `SONDER_ALLOW_CLOUD=1`; bounded to 2 concurrent by default
  (`max_cloud_workers`); **no failed cloud call is retried automatically**.
- Receipts are durable JSON (survive UI restarts); full receipts can contain
  answers, so `model_fanout_status` is owner-scoped and developer-gated on
  shared deployments. Answers pass prompt-echo and credential redaction
  (`_fanout_redact_prompt_echo`, server.py:23109).
- `profile` is exact and never a user selector: `healthy-local-chat`,
  `healthy-cloud-chat`, `healthy-chat`, or `loaded-local-chat`
  (server.py:22630-22649). `loaded-local-chat` is deliberately no-load: it
  fails closed unless every selected local model is already resident.
- Terminal receipts expire after 7 days (`SONDER_FANOUT_TTL_SECONDS` override).

## The retry / failover ladder

Layered from innermost out; each layer has one job and never widens the one
below it.

| Layer | When it fires | Bound |
|---|---|---|
| Loopback transient retry | Transport failure on a loopback single-host call | `SONDER_LOCAL_RETRIES` 0..2, default 1 (sonder_runtime/platform/local_retry_policy.py:8-18); shares the same monotonic timeout budget, checks fleet cancellation before each attempt, never changes endpoint/model/tier |
| Context-overflow compaction retry | Failure **text** classifies as overflow (sonder_runtime/domain/context/overflow.py) | Exactly one extra attempt (server.py:3860-3862); compaction drops oldest whole turns and inserts an in-band note, never rewrites content (domain/context/compaction.py:10-45); a single oversized turn is reported, not retried; body-too-large, device OOM, rate-limit, and missing-model are named negative controls that veto the retry |
| Hosted/cloud | Always | Single attempt - metered work is never silently duplicated - unless the call site declares `idempotent` AND the operator sets `SONDER_HOSTED_OVERFLOW_RETRY=1` (server.py:3835-3837; sonder_runtime/platform/model_retry_policy.py:10) |
| Pool failover | Transport error **before any response** was received | `_FAILOVER_HTTP_CODES` {502, 503, 504}, URLError/Timeout/Connection/OSError; a completed request (including a model error response) is never replayed on another host (ollama_pool.py:1-7, 22, 170-210) |

Key subtlety: an enabled worker pool is treated as a **remote route** for retry
policy even when the primary endpoint is loopback (server.py:3848-3858), so
`max_attempts` drops to 1 and only pre-response pool failover applies. HTTP
status is supporting evidence only for overflow classification - proxies lie
(a 429 can be a real overflow), so the decision is text-based and conservative
(overflow.py:1-33).

## Multi-PC Ollama worker pool

`sonder_runtime/adapters/inference/ollama_pool.py`. This is **request-level
pooling, not model-weight sharding**: each host runs its own Ollama and model
files; the coordinator picks one worker per request (README.md:309-312).

Configuration (runbook: `docs/runbooks/multi-pc-ollama.md`):

```powershell
$env:OLLAMA_HOST = "http://127.0.0.1:11434"
$env:SONDER_ALLOW_REMOTE_OLLAMA = "1"
$env:SONDER_OLLAMA_WORKERS = "https://ollama-pc2.example.internal:443;https://ollama-pc3.example.internal:443"
python -m sonder_runtime preflight
python -m sonder_runtime serve
```

- Origins are comma/semicolon separated, deduplicated; the primary endpoint
  counts as worker 1; the pool is `enabled` only with >1 distinct origin
  (ollama_pool.py:39-46, 127-146).
- `validate_worker_origin` (ollama_pool.py:49-64): no inline credentials, an
  explicit port is required, and any non-loopback origin requires **https plus
  `SONDER_ALLOW_REMOTE_OLLAMA=1`** - wrong consent/URL/TLS fails closed at
  startup. Keep worker Ollama on loopback behind a TLS reverse proxy; never
  port-forward raw 11434.
- Scheduling: least-inflight with cursor rotation among workers not in
  cooldown (ollama_pool.py:156-168). Circuit breaker: 3 consecutive transport
  failures -> 30 s cooldown (ollama_pool.py:22-24); a later success resets it.
- Each worker must have the exact model tags pulled (`ollama pull ...`);
  status/snapshots expose `worker_id`, `origin`, `healthy`, `inflight`,
  `consecutive_failures`, `cooldown_until` (ollama_pool.py:74-92).

## Safety boundaries (do not route around)

- `local_service_probe` is deliberately excluded from every agent/loop/
  autopilot tool surface; it exists only as a direct tool. This exclusion is
  pinned by tests (tests/test_local_service_probe_server.py:63-70).
- Whether an agent tool invocation mutates state is a pure domain policy:
  `sonder_runtime/domain/agent_mutation_policy.py` (`WORK_MUTATION_TOOLS`,
  `invocation_mutates`) - dry-run/preview modes do not count as mutation.
- Hosted (cloud-model) agent loops are denied every local-only/host-inspection
  tool, and the hosted tool manifest does not re-advertise them (pinned by
  tests/test_tool_capabilities.py:57-145).
- Autopilot refuses cloud tiers outright (autopilot_controller.py:68-74).
- Cloud consent (`SONDER_ALLOW_CLOUD`), remote-worker consent
  (`SONDER_ALLOW_REMOTE_OLLAMA`), and redaction rules are covered in
  sonder-security-and-privacy - never disable them to make a run pass.

## Troubleshooting quick table

| Symptom | Do |
|---|---|
| "agent stuck" / fleet row running with dead owner | `master_status()`; heartbeats (5 s) mark stale owners; on restart the claim reaper marks work interrupted - then `master_retry(<id>)` |
| Autopilot stuck after crash | `/autopilot status`, inspect, then explicit `/autopilot resume <id>`; a task shown `uncertain` was mid-flight and is intentionally not replayed |
| Cancel appears ignored | It is cooperative: an in-flight model call finishes and its result is discarded; check "active model calls awaiting return" in `master_cancel` output |
| Fleet slower than expected | `master_capacity()` - look at `bound_by`; if it warns about `OLLAMA_NUM_PARALLEL`, set it to the slot count and restart Ollama |
| Worker cap not honored | Only one unquoted, affirmative "use N workers" phrase parses; quotes, negation, or comparatives disable inference (master_orchestrator.py:471-488) - pass `worker_cap=` explicitly |
| Remote worker rejected at startup | Check https, explicit port, no inline credentials, `SONDER_ALLOW_REMOTE_OLLAMA=1`, model tag pulled on the worker |
| Protected fleet result rejected | Result lacks `=== TOOL EVIDENCE ===` from `file_read`/`file_read_range`/`text_search`/`script_search`, or the target changed mid-call - rerun; do not paraphrase evidence |
| Cloud fanout does nothing | `SONDER_ALLOW_CLOUD=1` unset (fails closed with an explicit message, server.py:22911) |

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Re-verify volatile facts from
the repo root:

- Autopilot bounds: `rg -n "MAX_TOTAL_CYCLES|MAX_ADAPTIVE_CHECKPOINTS|FAILURE_PREFIXES|TASK_KINDS" autopilot_controller.py`
- Capacity constants: `rg -n "ABSOLUTE_MAX_WORKERS|RAM_RESERVE_BYTES|GPU_KV_CACHE|HEARTBEAT_SECONDS|ABORT_MARKERS" master_orchestrator.py`
- Tool entry points: `rg -n "def master_orchestrate|def master_cancel|def autopilot_start|def model_fanout" server.py`
- Fleet store invariants: `rg -n "cancelled before model call|MESSAGE_MODES|retried_by|def claim_agent_messages" sonder_runtime/adapters/persistence/fleet_store.py`
- Provenance grammar: `rg -n "OBJECTIVE_MARKER|NEGATIVE_CLAIM|EVIDENCE_MARKER|_TARGET_EVIDENCE_TOOLS" fleet_provenance.py`
- Pool failover: `rg -n "_FAILOVER_HTTP_CODES|_DEFAULT_FAILURE_THRESHOLD|_DEFAULT_COOLDOWN_SECONDS|never replayed" sonder_runtime/adapters/inference/ollama_pool.py`
- Retry ladder: `rg -n "SONDER_LOCAL_RETRIES" sonder_runtime/platform/local_retry_policy.py; rg -n "SONDER_HOSTED_OVERFLOW_RETRY" sonder_runtime/platform/model_retry_policy.py; rg -n "OLLAMA_POOL.enabled" server.py`
- Reload split: `rg -n "LIVE_RELOAD_MODULES" server.py` (confirm `fleet_store` is absent from the list)
- Runbooks: `docs/runbooks/autopilot-interruption.md`, `docs/runbooks/multi-pc-ollama.md`, `docs/wiki/07-agent-autopilot-fleet.md`
