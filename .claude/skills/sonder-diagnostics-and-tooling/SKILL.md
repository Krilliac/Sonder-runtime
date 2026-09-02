---
name: sonder-diagnostics-and-tooling
description: >-
  Measure Sonder Runtime health with its built-in instruments instead of
  eyeballing: doctor/preflight/status anatomy, log and evidence locations,
  redaction guarantees, launcher health proof, metrics, and the read-only
  bench/eval scripts. TRIGGER when the user says "is the server healthy",
  "check status", "read the logs", "doctor output", "measure it", "inspect
  the db", "where do the logs go", or "run the health checks". DO NOT
  TRIGGER for symptom triage decision trees ("it's broken, why?") — those
  live in sonder-debugging-playbook; deep evaluation/promotion policy lives
  in sonder-validation-and-qa.
---

# Sonder Runtime: diagnostics and observation tooling

Sonder Runtime is a local-first Python 3.12 AI runtime wrapped around Ollama
(the local model server, default `http://127.0.0.1:11434`). This skill is the
instrument catalog: what each health surface measures, how to invoke it, what
healthy output looks like, and exactly where evidence lands on disk. Use it to
replace "it seems fine" with a number or a rendered report.

**When NOT to use this skill.** If you already have a failing symptom and need
a decision tree from symptom to cause, use `sonder-debugging-playbook`. If you
are deciding whether a model/adapter is good enough to promote, or designing an
evaluation, use `sonder-validation-and-qa` — this skill only points at the
measurement scripts. Environment setup and pytest gates are
`sonder-build-and-env`.

All commands below run from the repo root with the repo venv python
(`venv\Scripts\python.exe` on Windows); there is no installed package, so the
working directory matters.

## Instrument quick reference

| Instrument | Surface | Measures | Mutates? |
|---|---|---|---|
| `python -m sonder_runtime doctor` | CLI | 8-check consolidated health rollup | no (read-only by contract) |
| `python -m sonder_runtime preflight` | CLI | startup gate checks, no socket bound | creates state home + write probe |
| `python -m sonder_runtime status` | CLI | build / config / storage / schema status | no |
| `python -m sonder_runtime diagnostics` | CLI | redacted bundle (config+preflight+schemas), always JSON | runs preflight |
| `python -m sonder_runtime config` | CLI | effective redacted configuration | no |
| `python -m sonder_runtime smoke` | CLI | preflight + operations migration + DB write/read roundtrip | yes (migrates `operations`, writes one event) |
| `python -m sonder_runtime eval-history status` | CLI | identity-separated eval trends from JSONL | no |
| `status` / `diagnostics` | MCP tool | live server: models resident, workers, tiers, MCP registry | no |
| `log_inspect` | MCP tool | guarded, bounded log parsing (levels, clusters, repeats) | no |
| `self_heal_check` / `self_heal_repair` | MCP tool | local breakage; repair is dry-run unless `apply=true` | check: no; repair: only with `apply` |
| `mcp_runtime_status` / `live_reload_status` | MCP tool | tool-registry convergence, watched-module reload state | no |
| `evaluation_history_status` | MCP tool | eval trends; never runs a model | no |
| `debug_inspect` | MCP tool | developer-gated inspection bundle | no |
| `GET /` and `/v1/local/server-log` | HTTP loopback | live redacted server-log tail in a browser | no |
| `GET /v1/sonder/launcher-health` | HTTP | HMAC identity proof: is this listener *our* server | no |
| `sonder-headless-status.cmd` | wrapper | ollama + API listener + pid/log locations | no |
| `scripts/health-snapshot.ps1` (this skill) | helper | preflight+doctor+status saved to timestamped JSON | doctor/status legs: no; preflight leg creates state home + write probe |

## The doctor: anatomy of `python -m sonder_runtime doctor`

`sonder_doctor.py` (repo root) is read-only by documented contract: it never
repairs, writes, or opens sockets on its own; each default check guards its own
imports so a missing collaborator degrades to `skipped` instead of crashing. A
check that *raises* is captured as a `fail` entry naming the exception — one
broken collaborator never aborts the report.

The eight default checks, in report order (`default_checks()`):

| # | Check | What it verifies | Fail/warn semantics |
|---|---|---|---|
| 1 | `config` | config loads and validates | fail on config error |
| 2 | `storage_state` | state-home volume: free space, drive/filesystem type | warn on volume warnings |
| 3 | `storage_models` | every Ollama model root, same inspection | warn on volume warnings |
| 4 | `schemas` | migration history across all stores | **fail** if modified/future history; warn if pending |
| 5 | `self_heal` | summary of `self_heal.check` findings (no repair) | skip when `SONDER_DB` unset |
| 6 | `memory_quality` | `memory_quality.audit` over the memory store | skip when `SONDER_DB` unset |
| 7 | `runtime_policy` | policy loads (`create=False` — never creates the file) | warn = "policy error, safe defaults active" |
| 8 | `ollama` | `GET <ollama.url>/api/tags`, 5 s timeout | see below |

Rollup severity (`bootstrap/doctor_formatting.py`): `ok`=0, `skipped`=0,
`warn`=1, `fail`=2; overall = worst entry. **Exit code is 1 only when overall
is `fail`; warn exits 0** so scripts and CI can gate on real breakage without
tripping on advisories.

The `ollama` check's key distinction — reachable is not ready:

- Endpoint down / HTTP error → `fail` with `host: <error>`.
- Reachable with **zero models installed** → `warn`:
  `127.0.0.1: reachable, no models installed (run setup_alias.py)`.
  The runtime starts, but the next chat turn fails at the provider.
- Healthy → `ok` with `127.0.0.1: N models`.

Useful flags (verified in `sonder_runtime/__main__.py`):

```powershell
.\venv\Scripts\python.exe -m sonder_runtime doctor            # human-readable
.\venv\Scripts\python.exe -m sonder_runtime doctor --json     # machine-readable
.\venv\Scripts\python.exe -m sonder_runtime doctor --skip-ollama   # fully local
.\venv\Scripts\python.exe -m sonder_runtime doctor --storage-probe # add throughput numbers
python sonder_doctor.py    # thin direct wrapper, default checks only, no flags
```

`--storage-probe` is the **only** way to get throughput numbers: an explicit
benchmark capped at 8 MiB and 5 seconds (`PROBE_BYTES`,
`PROBE_TIMEOUT_SECONDS` in `sonder_runtime/adapters/storage.py`) against the
existing state directory only. It is never implied by `doctor`, `status`,
preflight, or service startup, and appends
`; explicit probe write=X.X MiB/s read=X.X MiB/s` to the `storage_state`
detail. Common flags on every subcommand: `--config <toml>`,
`--secrets <env>`, `--set section.key=value` (highest precedence), `--json`.

### What healthy doctor output looks like

Rendered by `render_report` as `sonder doctor: <OVERALL>` plus one
`[mark] name  detail` line per check (marks: `ok`, `warn`, `FAIL`, `skip`).
Captured from a real `doctor --skip-ollama` run on 2026-08-22 (paths will
differ; the `ollama` line shows its healthy format and is omitted under
`--skip-ollama`):

```
sonder doctor: OK
  [ok  ] config          ollama=http://127.0.0.1:11434
  [ok  ] storage_state   C:\Users\me\AppData\Local\sonder: 1532.8 GiB free, fixed/ntfs
  [ok  ] storage_models  D:\ollama\models: 967.6 GiB free, fixed/ntfs
  [ok  ] schemas         7 store(s) current; applied migrations=8
  [skip] self_heal       skipped: SONDER_DB not set
  [skip] memory_quality  skipped: SONDER_DB not set
  [ok  ] runtime_policy  revision=1 source=runtime_policy_update
  [ok  ] ollama          127.0.0.1: 3 models
```

`skip` does not degrade the rollup — a standalone CLI run legitimately skips
the two `SONDER_DB`-backed checks; inside a served runtime they run for real.
Treat any `FAIL` line, or overall `WARN` that mentions "no models installed"
or "pending migrations", as actionable before serving traffic.

## Preflight, smoke, status, diagnostics

**Preflight** (`sonder_runtime/adapters/preflight.py`, invoked by
`python -m sonder_runtime preflight` and automatically by `serve`) runs, in
order:

1. State home: `mkdir -p` then write/delete a `.sonder-write-probe` file.
2. Every `state.workspace_roots` entry is an existing writable directory.
3. Free disk on the state volume >= `state.minimum_free_disk_bytes`
   (default `5_368_709_120` = 5 GiB).
4. Per-store schema versions: **future (unknown) migrations or a modified
   migration history are required failures**; *pending* migrations are
   reported but non-required (run `migrate` to clear them).
5. Runtime policy loads (`revision=N` on success).
6. Optional Ollama `GET /api/tags` (skip with `--skip-ollama`).

Exit 0 when all required checks pass, 1 otherwise, 2 on config error. `serve`
startup order is preflight → migrations → bind; a failed required check means
no socket ever opens.

**Smoke** is the minimal end-to-end proof: preflight + `migrate_store("operations")`
+ one `OperationsStore` event written and read back. Success prints exactly
`smoke passed`. It is the cheapest "the storage stack actually works" check —
but note it *does* migrate the operations store and write one event, so it is
not read-only like doctor.

```powershell
.\venv\Scripts\python.exe -m sonder_runtime preflight --json
.\venv\Scripts\python.exe -m sonder_runtime smoke --skip-ollama
.\venv\Scripts\python.exe -m sonder_runtime status --json
.\venv\Scripts\python.exe -m sonder_runtime diagnostics --skip-ollama > diag.json
```

`status` reports build info, profile, config sources, storage inspection, and
per-store schema counts (`applied`/`pending`/`healthy`) and stays available
even when config fails (it reports `config_errors` instead). `diagnostics`
always emits JSON and only redacted configuration (`as_redacted_dict()`) — it
is the safe thing to attach to a bug report.

## MCP-side instruments (inside a running server)

These are MCP tools registered in `server.py`; from the REPL most are also
reachable as slash commands or plain English.

**`status`** — the live one-screen operational picture: unsafe-lab mode,
Ollama endpoint + locality (loopback vs remote), the worker pool line
`Ollama workers: N configured (M remote; least-inflight with transport
failover)`, tier→model table with `[local Ollama]` / `[CLOUD - leaves
machine]` / `[REMOTE OLLAMA - leaves machine]` markers, installed models,
**models resident in VRAM right now** (`/api/ps`), keep_alive, local runtime
threads/gpu_layers/batch, hardware + free VRAM, MCP provenance, autopilot
counts. Use it to check whether the GPU is busy before offloading work.

**`diagnostics`** — installation-health counterpart: unsafe-lab status,
live-reload on/off, Ollama endpoint + locality + remote opt-in, MCP runtime
(`N tools, M atomic refreshes, list-changed=on/off`, last refresh error),
tool-capability shadow/coverage, tool contract, runtime-policy
revision/path, local runtime knobs, retry policy, system profile.

**`log_inspect`** — the *guarded* way to read any log: fixed
level/timestamp/source extraction, failure clusters, repeat detection, bounded
context. No execution, no caller expressions. Hard bounds (defaults):

| Bound | Default |
|---|---|
| `max_file_bytes` | 64,000,000 |
| `max_scan_bytes` | 4,000,000 |
| `max_lines` | 10,000 |
| `max_line_bytes` | 4,096 |
| `max_results` | 100 |
| `max_output_bytes` | 256,000 |
| `timeout` | 5.0 s |

Prefer it over ad-hoc file reads: the bounds make it safe on a runaway log,
and it participates in the read-only inspection audit trail.

**`self_heal_check`** — reports common local breakage (missing lesson FTS
rows, orphan FTS rows, corrupt embeddings, invalid JSON config files, broken
venv, live-reload syntax errors) without changing anything.
**`self_heal_repair`** is dry-run by default; only `apply=true` performs the
safe subset of repairs, and Python/venv or syntax problems are reported, never
auto-fixed.

**`mcp_runtime_status` / `live_reload_status`** — tool-registry convergence
and per-module reload state. Updated tool implementations swap atomically;
invalid source keeps the last known-good registry, so a reload `ERROR:` line
here means you are running old code.

**`evaluation_history_status`** — reads eval trends only; identity-separated
(see evidence section) and it **never runs or promotes a model**.

**`debug_inspect`** — developer/admin-gated (requires a developer token)
bundle: admin status, MCP runtime, master status, improvement report, memory
quality sample. It refuses hidden chain-of-thought by design.

The REPL composer title is itself an instrument: it live-renders tier, model,
lanes/agents (`L2 A5`), context budget (`ctx~used/limit (left …)`), last-turn
tokens in/out, elapsed time, and model/tool call counts.

## Where evidence lands

State home resolution is owned by `sonder-run-and-operate` (env override,
then a per-OS default; the layer-dependent `SONDER_STATE_HOME` vs
`SONDER_HOME` alias trap is documented in `sonder-config-and-flags`).
Everything below is relative to that home.

| Path | Contents |
|---|---|
| `run/sonder_serve.log` | managed server stdout+stderr (headless launcher) |
| `run/ollama.log` | managed `ollama serve` output |
| `run/sonder_serve.pid`, `run/ollama.pid` | plain-integer PID files |
| `dumps/sonder-dump-<stamp>-<hex>.txt` | REPL `/dump [label]` debug dumps |
| `eval-history.jsonl` | append-only evaluation evidence |
| operations store (SQLite) | tool/operation audit events |

**Log dashboard.** With the server up, a loopback-only browser page at
`http://127.0.0.1:11435/` shows a live, read-only tail of
`run/sonder_serve.log`, fed by `GET /v1/local/server-log` (JSON `{"log": ...}`,
bounded tail, refreshed every second). Non-loopback peers get 404.

**Redaction is load-bearing, everywhere.** The server-log tail is passed
through the same conservative secret projection as activity output (quoted
assignments, bearer credentials, URI credentials, JWTs, provider keys). Debug
dumps are redacted by `platform.logging.Redactor` before writing, created
`0o600`, with server-generated names (labels never become filenames).
Structured logs come from `configure_logging(level, log_format)` — JSON lines
with UTC timestamps and redacted messages when `[observability].log_format =
"json"` (default), plain text when `"text"`. The operations store persists
**identifiers, counts, hashes, durations, and redacted paths only — never
prompts or memory text**. If redaction itself fails anywhere, the entire
detail is replaced by the literal `[REDACTION_FAILED]`: the system degrades
observability, never privacy. Seeing that marker means "redactor broke", not
"nothing happened".

**Evaluation history** (`sonder_runtime/adapters/evaluation_history_store.py`):
append-only JSONL, schema `sonder.eval-history.v1`, capped at 64 MiB and
10,000 records read by default (50,000 hard max). It records caller-supplied
aggregates only and reports trends strictly within an exact
model + model-digest + suite + suite-version + suite-digest identity — so a
"trend" can never mix two different models or suites. CLI:
`python -m sonder_runtime eval-history status --model X --model-digest <sha256> ...`.

## Launcher health proof: is that listener really our server?

`domain/launcher_health.py` defines an HMAC identity contract so the launcher
never trusts "something answered on the port":

- Request: `GET /v1/sonder/launcher-health` with header
  `X-Sonder-Launcher-Health-Nonce: <64 hex chars>`.
- Response payload: identity `sonder-launcher-health-v3`, service
  `sonder-serve`, role `managed-host`, pid, port, echoed nonce, and `proof` =
  HMAC-SHA256 over the canonical contract/identity/service/version/role/pid/
  port/nonce message, keyed by `SONDER_LAUNCHER_HEALTH_TOKEN`.
- Token: >= 32 chars; auto-provisioned by the launcher into a `0o600` token
  file (`sonder-launcher-health.token` beside the launcher DB) when not
  configured.

From this the launcher derives exactly three states: `healthy` (proof
verifies), `stopped` (port closed), `foreign_listener` (anything else —
wrong proof, oversized reply, HTTP error). **`foreign_listener` blocks
start/restart**: the launcher refuses to manage a port owned by a process it
cannot cryptographically identify. If you see it, find and stop whatever holds
port 11435 rather than forcing a start.

Headless wrapper status (no venv activation needed):

```
.\sonder-headless-status.cmd
```

prints ollama reachability + pid, whether the API listener on
`127.0.0.1:11435` is Sonder-managed or an *unmanaged listener*, the run dir,
and both log paths.

## Metrics and toggles

`sonder_runtime/platform/metrics.py` owns a bounded Prometheus registry
(`MetricsRegistry`) — label sets fixed at registration, no free-text labels,
and every call is a no-op when `prometheus_client` is not installed, so
metrics are never a hard dependency. Key series: `sonder_build_info`,
`sonder_process_state`, `sonder_requests_total{route,result}`,
`sonder_request_duration_seconds{route}`, `sonder_active_requests`,
`sonder_request_cache_total`. Configuration lives in `[observability]`
(`metrics_enabled` default true, `metrics_path` default `/metrics`,
`log_level`, `log_format`); the entry point exports it as the `SONDER_METRICS`
env toggle (`1`/`0`) for the legacy server code.

## Read-only bench/eval instruments

One line each; invocations verified from the scripts' own docstrings/parsers.
These *measure*; evaluation methodology and promotion decisions belong to
`sonder-validation-and-qa`.

| Script | Measures | Invoke |
|---|---|---|
| `scripts/benchmark_moat.py` | retrieval-augmentation lift: bare vs runtime-cold vs runtime-warm on one fixed model, deterministic graders | `python scripts/benchmark_moat.py [--json out.json --markdown card.md]` |
| `scripts/benchmark_adaptive.py` | model-free comparison of two recorded checkpoints (fresh vs accumulated history), identical model/suite/hardware identity required | `python scripts/benchmark_adaptive.py record ... / compare ...` |
| `scripts/benchmark_repository_research.py` | deterministic scoring of repository-research evidence; no model or network calls | `python scripts/benchmark_repository_research.py ...` |
| `scripts/benchmark_schema_offload.py` | did schema-constrained decoding help: valid/rejected/unusable/wrong/not_run kept strictly separate | `python scripts/benchmark_schema_offload.py` |
| `scripts/ci_watch.py` | recent GitHub Actions runs via `gh` | `python scripts/ci_watch.py [--repo R --branch B --limit N]` |
| `scripts/probe_slash_commands.py` | every read-only slash command against a running server; mutating commands never sent | `python scripts/probe_slash_commands.py [PORT] [--json OUT]` |
| `scripts/audit_live_slash_commands.py` | the server's advertised command catalog with bounded args; only `safe` commands by default | `python scripts/audit_live_slash_commands.py ...` |
| `eval_retrieval.py` | grounded pass-rate lift of lesson retrieval on held-out tasks, chunk-resumable | `python eval_retrieval.py [start] [count]` |
| `eval_solver.py` | pass@1 vs pass@repair — lift from execution-grounded self-repair | `python eval_solver.py [max_attempts]` |
| `eval_duel.py` | single-model vs cross-model repair strategies, execution-verified | `python eval_duel.py [n_tasks]` |
| `eval_models.py` | exact base-vs-candidate promotion suite; evaluation-only, never touches aliases | `python eval_models.py [base] [candidate] [--record-history]` |

`benchmark_schema_offload.py`'s five-way outcome split exists because this
codebase distrusts counts: a model call that never ran must not be scored as a
zero and averaged into a rate. When you report any benchmark number, report
`not_run` alongside it.

## Snapshot helper (this skill)

`scripts/health-snapshot.ps1` (co-located with this skill) runs
`preflight --json`, `doctor --json`, and `status --json` via an explicit
python and writes each report's stdout to `<stamp>-<name>.json` — with stderr
captured separately to `<stamp>-<name>.err.txt` so diagnostics noise never
corrupts the JSON — under a directory you supply. Mutation contract: doctor
and status are read-only; **preflight is not fully read-only** — it creates
the state home if missing and performs a write probe in it. It defaults to
the repo venv python when present:

```powershell
powershell -File .claude\skills\sonder-diagnostics-and-tooling\scripts\health-snapshot.ps1 `
  -OutputDir C:\temp\sonder-health
```

Exit 0 only when all three subcommands exited 0 (remember: doctor `warn` is
exit 0 by design). It never invokes migrate, smoke, repair, or the storage
probe.

## Measurement discipline

- Before believing a clean result, ask what it would look like if the thing
  were broken. `doctor` deliberately encodes this: a check that raises is a
  captured `fail`, not a silent skip; `skipped` states its reason.
- "Reachable" is not "ready" (the ollama zero-models warn), "listening" is not
  "ours" (`foreign_listener`), and an empty log tail may mean the log file
  does not exist yet — the dashboard prints
  `(server log is not available yet)` for that case, distinct from silence.
- Prefer `--json` outputs for comparisons across runs; prefer `log_inspect`
  bounds over raw reads; prefer recorded `eval-history` identities over
  remembered numbers.

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Re-verify each volatile claim:

- Doctor check order and read-only contract:
  `python -c "import sonder_doctor; print([n for n, _ in sonder_doctor.default_checks()])"`
  (expect config, storage_state, storage_models, schemas, self_heal,
  memory_quality, runtime_policy, ollama).
- Rollup/exit semantics: `grep -n "_SEVERITY" sonder_runtime/bootstrap/doctor_formatting.py`
  and `grep -n "overall.*STATUS_FAIL" sonder_doctor.py`.
- CLI flags: `python -m sonder_runtime doctor --help` and
  `python -m sonder_runtime --help` (subcommand list).
- Storage probe caps: `grep -n "PROBE_BYTES\|PROBE_TIMEOUT" sonder_runtime/adapters/storage.py`.
- Preflight checks and 5 GiB default:
  `grep -n "def _check_\|minimum_free_disk_bytes" sonder_runtime/adapters/preflight.py sonder_runtime/platform/config.py`.
- MCP tools present: `grep -n "^def status(\|^def diagnostics(\|^def log_inspect(\|^def self_heal_check(" server.py`.
- log_inspect bounds: `grep -n "max_file_bytes: int" server.py` (def at the `log_inspect` signature).
- Launcher health contract: `grep -n "IDENTITY\|MIN_TOKEN_LENGTH\|NONCE_HEADER" sonder_runtime/domain/launcher_health.py`.
- Eval-history caps/schema: `grep -n "SCHEMA\|MAX_HISTORY_BYTES\|DEFAULT_MAX_RECORDS" sonder_runtime/adapters/evaluation_history_store.py`.
- Evidence paths: `grep -n "sonder_serve.log" sonder_runtime/interfaces/http/serve.py`
  and `grep -n "def run_dir\|def log_file" sonder_headless.py`.
- Redaction sentinel: `grep -rn "REDACTION_FAILED = " sonder_runtime/platform/logging.py`.
- Default ports/paths: `grep -n "port: int = \|url: str = " sonder_runtime/platform/config.py`
  (server 11435, Ollama 11434).
