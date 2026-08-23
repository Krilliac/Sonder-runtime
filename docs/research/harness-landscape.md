# Agent-harness landscape and porting roadmap

Status: **research document** (docs-only; imposes no runtime behavior).
Date: 2026-08-22. Branch: `claude/fable-ecosystem-research`.

## Purpose and method

This document compares Sonder Runtime against the surrounding agent-harness
ecosystem across eight dimensions — execution model, state/durability, tools,
evals, memory, UI/surfaces, safety, and distributed operation — and turns the
comparison into an ordered, repo-grounded porting roadmap.

Method and provenance rules:

- **Sonder claims are grounded in this repository.** Every Sonder statement
  cites a file (and usually a line region) that was read during this survey.
  Line numbers drift; the file and symbol names are the durable anchors.
- **External claims come from each project's public code/docs as of early
  2026.** They describe each project's *signature mechanism*, not its full
  surface. Before implementing anything borrowed, re-verify against the
  upstream source; treat every external claim here as a pointer, not a spec.
- **Every recommendation is labeled** `adopt` (wire or build now), `adapt`
  (borrow the idea, reshape to Sonder's contracts), `defer` (blocked on a
  prerequisite), or `reject` (with the reason recorded so it is not silently
  re-proposed). Anything not yet proven in Sonder is **experimental**.
- Experiments follow
  [`templates/adoption-experiment.md`](templates/adoption-experiment.md),
  which carries the default no-go constraints (no new egress without an
  existing consent gate, no writes to `selfmod.protected_paths()`, no new
  runtime dependencies without a separate decision, no weakening of a
  deny-by-default path, ADR-005 immutable deployment identities).

Supporting artifacts in this directory:

- [`templates/adoption-experiment.md`](templates/adoption-experiment.md) —
  the per-mechanism experiment template.
- [`schemas/trace-event.schema.json`](schemas/trace-event.schema.json) —
  EXPERIMENTAL local trace-event shape aligned with OpenTelemetry GenAI
  semantic conventions (supports R3).
- [`schemas/golden-eval-case.schema.json`](schemas/golden-eval-case.schema.json)
  — EXPERIMENTAL golden eval-case shape borrowing Inspect AI's
  task/solver/scorer decomposition and Promptfoo's assertion style
  (supports R1).

---

## 1. Sonder baseline: what actually exists

The survey behind this section read the runtime directly. Three findings
frame everything below.

**First: Sonder's distinctive strengths are disciplines, not features.**

1. **The could-not-judge channel.** At every layer, "I could not check" is a
   separate value from "it failed": `VerifierUnavailable`
   (`verifiers.py:45-47`, with a documented real bug where a missing MSVC
   toolset was scored as failing C++), the three-state
   violated/satisfied/`unchecked` verdict in `json_schema_verifier.py`,
   `evaluation_infrastructure_error` in `promotion_eval.py:825-830` and
   `grounded_outcomes.py:141`, `UNVERIFIED`/`UNVERIFIABLE` in
   `calibration.py:82-86`, and `SYSTEM_OPERATION_UNBOUND` in
   `tool_contract.py:47`. Most external harnesses collapse this into
   pass/fail.
2. **Population separation with recorded provenance.** Outcome rows carry a
   NOT-NULL closed-vocabulary `source` (`caller|machine|attributed|
   self_curriculum|unknown`, `sonder_runtime/adapters/memory_store.py`
   schema), reward prices are frozen (`sonder_runtime/domain/memory/
   rules.py:15-32`), and `calibration.py` refuses to average self-graded and
   caller-judged populations. Self-invented curriculum tasks are recorded via
   `record_self_graded_outcome`, deliberately not `record_outcome`
   (`server.py` near `5794`).
3. **Crash-uncertainty semantics that refuse silent replay.** Fanout rows are
   leased and fenced (`fanout_store.py:543,557,583`); a crash after dispatch
   is recorded `unknown` with `failure_class='execution_uncertain'`
   (`fanout_store.py:745`) and `retry_unknown` defaults false so metered work
   is never replayed silently. Autopilot marks interrupted tasks uncertain and
   pauses for review instead of auto-replaying
   (`autopilot_controller.py:267,550-573`).

**Second: several of Sonder's most sophisticated mechanisms are built,
tested, and unwired.** This changes the roadmap fundamentally — much of the
"porting" work is actually *wiring* work on assets that already match the
external state of the art:

| Unwired asset | What it is | Evidence |
|---|---|---|
| Trajectory replay | `sonder.evaluation-trajectory.v1` records with SHA-256 step digests and `replay_trajectory()` comparison | `sonder_runtime/application/evaluation/trajectory_replay.py:1-49`; no production capture path feeds it |
| Context planner | Ten-section budgeted prompt assembly (incl. `repository_map`, `subagent_results`) with protected-item eviction and per-item `SelectionExplanation` | `sonder_runtime/application/context_planner.py:15-26`, `domain/context/priority.py`; `ContextPlanningFacade.assemble` has no production caller |
| Lesson decay + contradiction detection | Exponential half-life ranking (30d), usage credit, opposite-polarity contradiction pairs at sim ≥ 0.8 | `lesson_decay.py:22,56-75,211`; retrieval never consults it |
| Transactional outbox | `OutboxWriter`/`OutboxDispatcher` with `UNIQUE(source_event_id)` dedup, per ADR-004 | `sonder_runtime/adapters/sqlite/outbox.py:41-151`; `dispatch_batch` has no production caller |
| Queued-action ledger | Idempotent `request_id`+payload-digest replay, SQL-enforced state machine, append-only triggers | `queued_actions.py:136-209,373-399`; no production caller routes through it |
| OTel-shaped tracing | Export-neutral trace projection + `TraceExporter` protocol with privacy/cardinality caps | `sonder_runtime/application/observability/trace_projection.py`, `operations/tracing_health.py`; deliberately no exporter |
| Eval history store | Append-only `sonder.eval-history.v1` JSONL keyed on exact model+digest+suite+digest identity | `sonder_runtime/adapters/evaluation_history_store.py:1-33`; the `eval_*.py` CLIs don't write to it |

**Third: the sharpest genuine gaps** (things neither wired nor built):

- No golden/regression eval suite beyond `promotion_eval.py`'s four SQL tasks
  (the C++ eval in `contrib/cpp_coding_eval_2026-07-06.md` is a write-up; its
  33 tasks are not in the repo). The `eval_*.py` scripts print to stdout with
  no structured records.
- No token or cost budgets anywhere: tokens are estimated (`len/4`,
  `master_orchestrator.py:845`) and logged, never enforced; fanout admission
  explicitly returns `"provider_pricing": "not_estimated"`
  (`server.py:~23370`).
- No task decomposition: `master_orchestrator._subtask_prompts` sends N
  *identical* prompts differing only by a subagent-index string
  (`master_orchestrator.py:1284,1327`); the only partitioning is round-robin
  over caller-written `[objective:<id>]` markers (`:1344-1352`).
- No persistent, ranked code map: `symbol_index.py` re-parses on every call,
  returns declarations in discovery order, and nothing fills the context
  planner's `repository_map` section.
- No cross-run trace persistence: the trace buffer holds 8 turns in memory
  (`adapters/observability/trace_buffer.py:6-28`); `turn_inspect` evidence
  dies with the process.
- Retrieval is a brute-force cosine scan over all lessons
  (`retriever.py:189-219`) — fine at ~10³ lessons, not at 10⁵.
- Multi-PC pooling (`sonder_runtime/adapters/inference/ollama_pool.py`) is
  model-blind: a worker missing the model 404s and the call fails (404 is not
  failover-eligible, `:22,170-174`); pooled `/api/ps` can describe a
  different machine than the one that executes (`server.py:~23275`); the
  orchestrator's capacity math is single-host (`master_orchestrator.py:491`).
- Saved workflows are flat action lists that never pass a `cancel_check`, so
  a running workflow is uncancellable
  (`application/workflows/loop.py:57` vs `server.py:~13945`).

### Baseline by dimension

| Dimension | Sonder today (grounded) |
|---|---|
| Execution model | Single agent loop; durable Autopilot (lease-claimed, checkpointed, budget-bounded cycles, `autopilot_controller.py:524-757`); two-level fleet fan-out + single audit merge (`master_orchestrator.py:1356-1643`); all-models fanout with sealed target snapshots (`server.py:23187+`); read-only speculative tool execution (`sonder_speculation.py`) |
| State | SQLite everywhere with WAL, leases, owner-PID fencing, CAS transitions (`fleet_store.py`, `fanout_store.py`, `autopilot_store.py`); checksummed migrations with immutable replay; outbox/queued-actions built but unwired |
| Tools | ~205 `@mcp.tool()` functions in `server.py`; risk catalog (`command_registry.py`); live capability manifest with SHA-256 (`server.py:15185+`); shadow capability descriptors covering 15 of ~184 tools (`tool_capabilities.py:177-233`); derived authority contract that fails closed on drift (`tool_contract.py`) |
| Evals | Deterministic anti-memorization SQL promotion gate (`promotion_eval.py`); pluggable verifier registry with could-not-judge channel (`verifiers.py:224-296`); automatic reward attribution (`grounded_outcomes.py`); ad-hoc `eval_*.py` CLIs; ~800-file pytest suite with substantive drift/conformance tests |
| Memory | Hybrid FTS5+cosine retrieval with RRF k=60, min-sim 0.62 floor and 0.70 uncorroborated floor, MMR diversification, statistical quarantine (`retriever.py`); distillation with vagueness/anchor gates and fail-closed embedding-provenance dedup (`reflection.py`); tombstones prevent re-learning rejected values |
| UI/surfaces | MCP (hot-reload, fail-closed swap, `reloadable_mcp.py`), HTTP with role-bound operations (`interfaces/http/serve.py`), REPL, A2A JSON-RPC facade (`interfaces/a2a`, `tests/test_a2a_*`), Flutter app consuming `permission_mode_data()` |
| Safety | Two-axis permissions (autonomy modes × privilege) with fail-closed `UNCLASSIFIED` and non-interactive ASK→ALLOW degrade honestly documented (`permission_modes.py:238-242,564-682`); selfmod state machine with hash-verified backups and rollback rehearsal (SELFMOD.md); container `isolated_runner` (network=none, cap-drop=ALL) unavailable to agents |
| Distributed | Multi-PC Ollama transport pooling (least-inflight, passive health, HTTPS+consent-gated remotes, `ollama_pool.py`); remote/cloud egress behind explicit env gates; no distributed execution, by design |

---

## 2. The landscape, by category

Verdicts reference the numbered recommendations in §4 and rejections in §5.

### 2.1 Coding-agent harnesses

**OpenHands** (ex-OpenDevin). Signature mechanism: a single append-only
*event stream* as the source of truth — every agent action and observation is
an event, which makes trajectories replayable and the UI a projection of the
stream. Sandboxed Docker runtime per session. Relevance: Sonder already has
the ingredients (activity spans, `trajectory_replay`, operations events) but
no unified persistent stream. → informs **R2/R3**. Its per-session Docker
runtime is heavier than Sonder's opt-in `isolated_runner`; not adopted.

**SWE-agent.** Signature mechanism: the *Agent-Computer Interface* — a small,
deliberately designed tool surface (search with bounded results, a windowed
file viewer, an edit command with lint-guarded rejection) measurably
outperforming raw shell access. Relevance: validates Sonder's guarded-tool
philosophy; the specific lesson worth porting is *lint-guarded edits* (reject
an edit that introduces a syntax error, with the error shown) — Sonder's
`file_edit` does not do this, though `ruff_verifier.py` exists. → **R10**.

**Aider.** Signature mechanisms: (a) the *repo map* — tree-sitter symbol
extraction ranked by a graph algorithm over the reference graph, fitted to a
token budget; (b) benchmarked *edit formats* (whole/diff/udiff) chosen per
model; (c) git-native workflow with auto-commits. Relevance: Sonder's
`symbol_index.py` is the unranked, uncached half of (a), and the context
planner reserves a `repository_map` section no producer fills. → **R5**.

**Goose** (Block). Local-first agent whose extensions *are* MCP servers;
recipes for repeatable runs. Relevance: same architectural bet Sonder made
(MCP as the tool substrate); its recipe format is a simpler cousin of
Sonder's saved workflows. Nothing to port beyond confirmation.

**Cline.** Signature mechanisms: plan/act mode split with per-tool-use human
approval, and *workspace checkpoints* (snapshot + restore of the working tree
per step). Relevance: Sonder's `plan`/`manual`/`acceptEdits`/`auto` modes
(`permission_modes.py:214-221`) already cover the approval spectrum;
workspace checkpoints overlap with selfmod's backup bundles but per-step
checkpointing for *ordinary* agent edits is absent. → considered, **defer**
(§5): selfmod's backup discipline covers the high-risk path; per-step
snapshots for ordinary edits need a cost/benefit measurement first.

**Continue.** Config-driven IDE assistant with codebase indexing (embedding
index + reranking). Relevance: Sonder embeds only lessons and interaction
tasks, never code. A code vector index is deliberately *not* recommended
before the repo map (R5): a ranked symbol map is cheaper, deterministic, and
serves the local models Sonder targets better than semantic code search at
current corpus sizes.

### 2.2 Orchestration frameworks

**LangGraph.** Signature mechanisms: explicit state-machine graphs; durable
*thread-scoped checkpointing* of every super-step enabling pause/resume,
human-in-the-loop interrupts, and time-travel replay. Relevance: Sonder's
fleet is deliberately not a graph (fixed fan-out + audit,
`master_orchestrator.py:1356-1643`) and should stay that way (§5), but
LangGraph's checkpoint-every-step durability is the strongest external
argument for wiring `workflow_checkpoints` and the outbox → **R7**, and its
interrupt pattern maps onto Autopilot's existing `control_flags` poll.

**AutoGen** (autogen-core/AG2). Actor-model async messaging, group chats,
nested conversations. Relevance: free-form multi-agent conversation is
rejected (§5) — it is unbudgeted and evades `fleet_provenance`'s evidence
receipts. The actor abstraction itself adds nothing over Sonder's
ledger-mediated workers.

**CrewAI.** Role-based crews with sequential/hierarchical processes.
Relevance: the "hierarchical manager" pattern is what
`master_orchestrator` already is, minus real decomposition. The useful
delta is planner-generated task lists → **R6**.

**OpenAI Agents SDK.** Signature mechanisms: minimal agents-with-handoffs;
*guardrails* as typed validators on input/output; built-in tracing; and
usage/limit plumbing. Relevance: guardrails ≈ Sonder's verifier registry at
the tool boundary (already stronger); the *usage-limit* discipline —
enforce, don't just log — is exactly Sonder's missing budget layer → **R4**.

**Semantic Kernel.** Enterprise plugin/planner framework with
filters/middleware. Relevance: its filter pipeline is a weaker version of
Sonder's entry-point permission gates (`permission_modes.py:64-86`). Nothing
to port; do not add as a dependency.

**smolagents.** Code-as-action: the model writes Python that *is* the action,
executed in a sandbox. Relevance: rejected for Sonder's default surface
(§5) — it dissolves the per-tool risk classification that
`permission_modes.risk_of()` depends on. Sonder's `run_code` already covers
the explicit-code case behind an `execution`-class gate.

**PydanticAI.** Signature mechanism: typed structured outputs where a
validation failure is fed back to the model as a *retry with the error*
(model-retry pattern). Relevance: Sonder validates JSON at many boundaries
(`json_schema_verifier.py` three-state; fence-tolerant agent JSON parsing)
but does not systematically loop validation errors back for one bounded
retry. → **R10** (as the same "machine-checked feedback, one bounded retry"
family as lint-guarded edits; Sonder's overflow-compaction retry at
`domain/context/overflow.py` shows the house style: classified error →
single bounded retry).

**DSPy.** Declarative signatures compiled against metrics (prompt/few-shot
optimization). Relevance: **defer** (§5) — meaningless without a golden
suite to optimize against (R1 is the prerequisite), and Sonder's
hand-written prompt gates (`reflection.py:9-21` bans vague words) encode
judgments an optimizer would need a metric to preserve.

### 2.3 Memory systems

**Letta/MemGPT.** Signature mechanisms: a memory *hierarchy* (small
always-in-context core blocks; archival storage paged in via tools) and
*self-editing* memory (the agent rewrites its own core blocks); sleep-time
processing. Relevance: Sonder's split is philosophically different and
better-evidenced — facts are asserted by the operator, lessons are *earned*
through graded outcomes (`server.py:12471+`, reward table in
`domain/memory/rules.py:18-32`). Self-editing core memory is rejected (§5).
The genuinely portable ideas: recency/decay weighting in ranking (Sonder
built it and never wired it → **R8**) and the paged always-present core
(which the unwired context planner's protected sections already model → R9).

### 2.4 RAG / pipeline frameworks

**LlamaIndex** and **Haystack.** Document/node abstractions, ingestion
pipelines, pluggable vector stores, eval hooks (Haystack pipelines are
typed component DAGs). Relevance: Sonder is not a RAG framework and should
not become one; its lesson corpus is small and curated by grading. The one
pressure point is ANN scaling of `retriever.py`'s brute-force scan —
explicitly **defer** behind a measured trigger (§5): adopt an index (e.g.
`sqlite-vec`) only when measured retrieval latency at the real corpus size
breaches a threshold, because it adds a native dependency.

### 2.5 Benchmarks and eval harnesses

**SWE-bench** (+Verified). Real GitHub issues; the oracle is fail→pass tests
in containerized, pinned environments. The lesson: *execution is the grader*
— which Sonder already lives by (`verifiers.py`, `grounded_outcomes.py`).
The portable discipline is environment pinning + a versioned task registry
→ **R1**.

**tau-bench.** Tool-agent-user with a *simulated user* and policy
compliance; signature metric **pass^k** — pass all of k independent trials —
measuring reliability, not peak capability. Relevance: pass^k is the right
antidote to `promotion_eval`'s single greedy sample; cheap to add because
the suite is deterministic-graded → **R1** acceptance criteria.

**Inspect AI** (UK AISI). Task = dataset + solver + scorer; every run
produces a complete structured eval log (transcripts, scores, metadata)
that a viewer can replay; built-in retry/resume and sandbox plumbing.
Relevance: this is the closest external analog to what Sonder's eval lane
should become, and the direct model for
[`schemas/golden-eval-case.schema.json`](schemas/golden-eval-case.schema.json)
→ **R1/R2**.

**OpenAI Evals / Promptfoo / DeepEval / Ragas.** Registry-of-YAML evals;
declarative assertions in CI with red-team modes; pytest-native LLM metrics;
RAG-specific metrics (faithfulness, context precision/recall). Relevance:
Promptfoo's "eval as CI gate" shape fits Sonder's pytest culture (the
`model` marker already exists, `conftest.py:96-113`); Ragas-style retrieval
metrics map onto `eval_retrieval.py`'s A/B design but would need the R1
scoreboard to matter. Model-graded metrics (G-Eval et al.) are constrained
by Sonder's own rule: `llm_judge` is a weak oracle
(`verifiers.py:202-221`) and must stay advisory → encoded in the schema's
`advisory` flag, and → **R11** (judge calibration).

**BrowserGym / WebArena / OSWorld / AgentBench / GAIA.** Environment suites
for web/OS/general agents. Relevance: not Sonder's product surface; no
adoption. Their common design lesson — execution-based checks over
declared-success — is already Sonder's `record_outcome` doctrine. GAIA's
level structure and OSWorld's per-task setup/teardown scripts are useful
references if the game-ladder gauntlet (`game_ladder.py:1-70`) is ever
generalized.

### 2.6 Observability and eval platforms

**LangSmith / Braintrust.** Hosted tracing + datasets + replay/playground
loops. Relevance: the *data model* (trace → dataset case → eval run →
regression diff) is the pattern R1+R2+R3 assemble locally; the hosted form
is rejected outright (§5) — Sonder's privacy boundary is the product.

**OpenTelemetry** (GenAI semantic conventions). Standard `gen_ai.*` span
attributes, metrics, and events for LLM calls and agents. Relevance: Sonder
already built OTel-*shaped* records with a `TraceExporter` protocol and
explicit privacy caps (`tracing_health.py`), stopping — deliberately —
before any exporter. Adopting the *naming* without any collector keeps
future interop free → **R3** and
[`schemas/trace-event.schema.json`](schemas/trace-event.schema.json).

### 2.7 Protocols

**MCP.** Sonder is already MCP-2.x-native with a hot-reload registry that
swaps fail-closed (`reloadable_mcp.py:1-11`, `docs/MCP_2_MIGRATION.md`), and
additionally ships an A2A JSON-RPC facade (`tests/test_a2a_*`). No adoption
gap. The open item is Sonder-internal: shadow capability descriptors cover
15 of ~184 tools (`tool_capabilities.py`, coverage fraction reported first
for exactly this reason) → **R12**.

---

## 3. Comparison matrix

"Sonder" cells are grounded in §1; external cells are signature-mechanism
summaries (verify upstream before porting).

| Dimension | Sonder today | Strongest external reference | Gap direction |
|---|---|---|---|
| Execution model | Fixed fan-out + audit; durable Autopilot loop; no graphs, no decomposition | LangGraph (checkpointed state machines), OpenHands (event stream) | Keep shape; add planner-generated objectives (R6), keep graphs out |
| State/durability | Lease-fenced SQLite, CAS transitions, crash-uncertainty semantics; outbox/queued-actions unwired | LangGraph checkpoints; Inspect AI eval logs | Wire what's built (R7); Sonder's uncertainty semantics are *ahead* |
| Tools | ~205 MCP tools, risk classes, derived authority contract failing closed | SWE-agent ACI; PydanticAI typed retries; MCP itself | Close descriptor coverage (R12); lint-guarded edits + validation retry (R10) |
| Evals | Deterministic promotion gate; verifier registry; no golden suite, no scoreboard | Inspect AI (task/scorer/logs); tau-bench (pass^k); SWE-bench (env pinning) | Largest gap: build the golden lane (R1), wire history + replay (R2) |
| Memory | Graded, provenance-separated lessons; hybrid retrieval + quarantine; decay unwired | Letta (hierarchy, recency); Aider (repo map) | Wire decay (R8); build repo map (R5); reject self-editing memory |
| Adaptive context | Reactive overflow compaction (one bounded retry); section planner built, unwired | Letta core/archival paging; Aider budget-fitted maps | Wire planner in shadow (R9) |
| UI/surfaces | MCP + HTTP + REPL + A2A + Flutter, one permission gate per entry point | Cline (approval UX), OpenHands (stream-projected UI) | No structural gap; evidence UX depends on persistent traces (R3) |
| Safety | Two-axis permissions, fail-closed unknowns, selfmod state machine with rollback rehearsal | OpenAI Agents SDK guardrails; smolagents sandboxing | Sonder is ahead; keep budgets as the missing enforcement (R4) |
| Distributed | Transport-level worker pooling, consent-gated egress | (No good external analog in this list; Ray-style schedulers out of scope) | Make pooling model-aware, fix `/api/ps` observability (R13) |
| Observability | In-memory rings; OTel-shaped records, no persistence | OTel GenAI semconv; LangSmith data model (not its hosting) | Persist locally (R3) |

---

## 4. Recommendations

Ordered by leverage. Each is one experiment under
[`templates/adoption-experiment.md`](templates/adoption-experiment.md); IDs
are reserved here so later documents can cite them. All are **experimental**
until their acceptance criteria are met. The default no-go constraints from
the template apply to every row; only *additional* no-gos are listed.

### R1 — Golden regression eval lane (EXP-001) — adopt

- **Borrowed from:** Inspect AI (task/solver/scorer + structured logs),
  Promptfoo (declarative assertions in CI), tau-bench (pass^k), SWE-bench
  (pinned environments, versioned registry).
- **Sonder grounding:** extends `promotion_eval.py`'s existing discipline
  (deterministic scorers, suite hashing, infrastructure-error channel) to a
  general case registry; reuses `verifiers.py` as the scorer registry and
  `tests/` conventions (the `model` marker gate, `conftest.py:96-113`).
  Case shape: [`schemas/golden-eval-case.schema.json`](schemas/golden-eval-case.schema.json).
- **Next step:** commit ~20 seed cases harvested from real graded outcomes
  (`memory.db` outcomes with `tests_passed`) plus the regression classes
  named in `contrib/cpp_coding_eval_2026-07-06.md`; a runner script that
  executes cases via the existing lanes and scores with `verifiers.py`.
- **Acceptance:** RED proof (a deliberately broken case fails); runner
  reports pass^3 per case; zero flaky cases across 3 consecutive full runs;
  `model`-marked so default CI is unaffected.
- **No-go (additional):** model-graded scorers may never gate (schema
  enforces `advisory: true` for `model_grader`).

### R2 — Wire the eval scoreboard and trajectory capture (EXP-002) — adopt

- **Borrowed from:** Inspect AI eval logs; LangSmith/Braintrust
  run-over-run regression diffing (data model only).
- **Sonder grounding:** both halves already exist unwired:
  `evaluation_history_store.py` (append-only, identity-pinned trends) and
  `trajectory_replay.py` (`sonder.evaluation-trajectory.v1`, step digests).
  The `eval_*.py` CLIs (`eval_duel.py`, `eval_retrieval.py`,
  `eval_solver.py`) print to stdout and record nothing.
- **Next step:** make each `eval_*.py` CLI write one eval-history record and
  one trajectory record per run; add a `--compare <prior-run>` flag using
  `replay_trajectory`.
- **Acceptance:** running any eval twice produces a machine diff naming
  regressed cases; history survives process restart; no schema change to
  either existing store format.
- **No-go (additional):** history/trajectory files stay under
  `SONDER_HOME`, never in-repo.

### R3 — Persistent local trace store on OTel GenAI names (EXP-003) — adapt

- **Borrowed from:** OpenTelemetry GenAI semantic conventions (`gen_ai.*`
  attributes); OpenHands' event-stream-as-truth.
- **Sonder grounding:** `trace_projection.py` (export-neutral, 256-span cap)
  and `tracing_health.py` (`TraceExporter` protocol, `_SENSITIVE_KEYS`,
  cardinality caps) are built; the 8-turn `trace_buffer.py` and
  `activity_tracker.py` rings are the in-memory sources; WP4 correlation IDs
  already exist. Proposed event shape:
  [`schemas/trace-event.schema.json`](schemas/trace-event.schema.json).
- **Next step:** implement one `TraceExporter` that appends sanitized events
  to a bounded local store (SQLite or JSONL under `SONDER_HOME`), default
  off behind an env flag; extend `turn_inspect` to read it after restart.
- **Acceptance:** a turn traced before restart is inspectable after restart;
  redaction test proves prompt text never lands in the store; bounded size
  with age/size retention like `selfmod_events`.
- **No-go (additional):** no network exporter of any kind — the OTLP wire
  format is explicitly out of scope; only the *names* are borrowed.

### R4 — Enforced token/cost budgets (EXP-004) — adopt

- **Borrowed from:** OpenAI Agents SDK usage limits; LangGraph
  budget/recursion caps; the general "enforce, don't log" discipline.
- **Sonder grounding:** estimation exists (`estimate_tokens`,
  `master_orchestrator.py:845`; fleet snapshots sum it); admission control
  exists for concurrency (`capacity()`, `:491`) but nothing enforces a
  per-run token ceiling; fanout declines to estimate price by design
  (`server.py:~23370`).
- **Next step:** add a per-master-run estimated-token ceiling checked at the
  same seams that already poll cancellation (`_begin_model_call`,
  `master_orchestrator.py:991`); exceeding it cancels remaining workers
  through the existing ledger flag and records a distinct terminal status
  (not `failed`).
- **Acceptance:** RED proof (a run configured with a tiny budget stops early
  with the new status); zero behavior change when the ceiling is unset;
  budget-stop is distinguishable from failure in `master_status`.
- **No-go (additional):** keep fanout's honest `not_estimated` posture for
  cloud *price* — enforce tokens, do not fabricate dollar estimates.

### R5 — Ranked, cached repository map (EXP-005) — adapt

- **Borrowed from:** Aider's repo map (reference-graph ranking fitted to a
  token budget).
- **Sonder grounding:** `symbol_index.py` (bounded, non-executing extraction
  for 8 languages) is the extractor; the unwired context planner reserves
  the `repository_map` section (`context_planner.py:21`).
- **Next step:** add an mtime-keyed cache and a dependency-free ranking pass
  (import/reference counting; PageRank only if it needs no new dependency),
  emitting a budget-fitted map; expose via the existing
  `repository_symbol_index` tool before touching any prompt path.
- **Acceptance:** warm call ≥10× faster than cold on a ≥500-file repo;
  ranking places entry-point/hub symbols above leaf declarations on a
  hand-labeled fixture repo committed to `tests/fixtures`; map text fits a
  declared token budget with explicit truncation reporting.
- **No-go (additional):** no tree-sitter or other native dependency; stdlib
  `ast` + existing regex extractors only.

### R6 — Planner-generated objective contracts (EXP-006) — adapt

- **Borrowed from:** CrewAI hierarchical process; LangGraph explicit-state
  decomposition — but *not* their engines.
- **Sonder grounding:** the fleet already validates objective contracts
  end-to-end (`fleet_provenance.parse_objectives` max 32, `task_digest`,
  fail-closed `TASK_DRIFT` aggregation, `master_orchestrator.py:1558-1578`);
  what's missing is a producer: today objectives exist only when the caller
  hand-writes `[objective:<id>]` markers.
- **Next step:** an optional pre-phase where one model call decomposes the
  master task into ≤N objective markers, which then flow through the
  *existing, unchanged* round-robin assignment and provenance gates; off by
  default.
- **Acceptance:** on a 3-part benchmark task, decomposed runs beat
  identical-prompt runs on audit-pass rate over ≥10 trials; a planner
  failure degrades to today's identical-prompt behavior, never to a stalled
  run.
- **No-go (additional):** the planner may only emit objective markers —
  never worker prompts, tool selections, or budget changes; `fleet_provenance`
  validation must remain the unchanged authority.

### R7 — Wire the outbox and retire dead ledgers decisively (EXP-007) — adopt

- **Borrowed from:** LangGraph durable checkpointing as external validation;
  ADR-004 already specifies the design internally.
- **Sonder grounding:** `sqlite/outbox.py` (dedup on `source_event_id`) has
  a writer imported only by `adapters/memory_repository.py` and a
  `dispatch_batch` no production code calls; `queued_actions.py` and
  `refinement_transactions.py` are fully built with no production route.
- **Next step:** decide per ledger: wire a minimal dispatcher loop for the
  outbox (drain into `operations.db` on the existing maintenance cadence),
  and either route one real approval flow through `queued_actions` or record
  an ADR retiring it. Unwired safety-critical-looking code is a hazard: it
  reads as protection it does not provide.
- **Acceptance:** outbox events written during a test session appear in
  `operations.db` after the dispatch cadence; kill-during-dispatch test
  shows no duplicate projection (the `UNIQUE(source_event_id)` path proven
  by an actual crash test, not inspection).
- **No-go (additional):** dispatcher must be idle-cost-free (no polling
  thread when the outbox is empty at startup and nothing writes).

### R8 — Wire lesson decay into ranking (EXP-008) — adopt

- **Borrowed from:** Letta/MemGPT recency weighting (concept only).
- **Sonder grounding:** `lesson_decay.py` is complete (half-life 30d, usage
  credit, `effective_score`, contradiction pairs) with tests and no caller;
  `retriever.py` ranking is similarity+usage-boost only; contradictions are
  detected by a function nothing invokes.
- **Next step:** shadow mode first: log, per retrieval, how the injected set
  *would* change with decay-weighted ranking; evaluate against
  `eval_retrieval.py`'s A/B harness (which exists and has held-out tasks).
- **Acceptance:** decay-ranked retrieval is non-inferior on the
  `eval_retrieval` pass-rate lift and reduces stale-lesson injections on a
  constructed fixture (old bad lesson vs new good lesson on the same topic);
  contradiction pairs surface in `memory_quality_report`.
- **No-go (additional):** no automatic resolution of contradictions —
  report only, per the existing dry-run-by-default house rule.

### R9 — Shadow-wire the section-budgeted context planner (EXP-009) — adopt

- **Borrowed from:** Letta's core/archival hierarchy; Aider's budget
  fitting; the general adaptive-context direction — all already embodied in
  Sonder's own unwired code.
- **Sonder grounding:** `ContextPlanningFacade.assemble`
  (`context_planner.py`) with per-section caps and `SelectionExplanation`
  has no production caller; the live prompt path is `orchestrator.py`'s
  `_draw_facts` + `MAX_TURNS=12` + incremental summary; `bootstrap/app.py`
  reads a `last_good()` that is always `None`.
- **Next step:** run the planner in shadow on real turns: assemble, diff
  against the actually-sent prompt, log divergence and the planner's
  explanations; no behavior change.
- **Acceptance:** ≥95% of shadow assemblies satisfy every section cap with
  zero protected-item evictions on normal turns; divergence report
  identifies at least one concrete improvement (e.g. facts crowding lessons)
  before any cutover is proposed.
- **No-go (additional):** cutover is a separate future experiment; this one
  is observation-only.

### R10 — Machine-checked feedback loops at the edit/output boundary (EXP-010) — adapt

- **Borrowed from:** SWE-agent's lint-guarded edits; PydanticAI's
  validation-error retry.
- **Sonder grounding:** the pattern already exists once, in house style:
  classified overflow → one bounded compaction retry
  (`domain/context/overflow.py`, `server.py:3860+`). `ruff_verifier.py` and
  `json_schema_verifier.py` are the checkers; `file_edit` and structured
  agent outputs are the boundaries that don't yet loop failures back.
- **Next step:** for agent-lane `file_edit` on Python files, run the cheap
  syntax check post-edit; on failure, revert the edit and return the error
  as the tool result (no model retry loop inside the tool — the agent loop
  decides). Same shape for one structured-output call site.
- **Acceptance:** RED proof with a deliberately syntax-breaking edit
  (reverted, error surfaced); no latency regression on clean edits beyond
  the compile check; repair rate on a 20-case broken-edit fixture improves
  vs baseline.
- **No-go (additional):** never auto-retry inside the tool body; one check,
  one honest report — retries belong to the caller.

### R11 — Judge calibration (EXP-011) — adapt

- **Borrowed from:** DeepEval/Ragas grader-quality practices; Inspect AI's
  human-agreement workflows.
- **Sonder grounding:** `llm_judge` (`verifiers.py:202-221`) is explicitly a
  weak oracle with no agreement measurement; `calibration.py` already has
  the population discipline (`MIN_SAMPLE=20`, fail-closed `unmeasured`) to
  hold the results.
- **Next step:** on cases where a deterministic verifier *and* `llm_judge`
  both apply (R1 corpus), record judge-vs-oracle agreement per rubric;
  publish as a new calibration population, never averaged into others.
- **Acceptance:** agreement rate with n≥20 exists per rubric; below-0.85
  rubrics are flagged in `calibration_status`; no gating change anywhere.

### R12 — Close the capability-descriptor coverage gap (EXP-012) — adopt

- **Borrowed from:** MCP's capability-declaration direction; PydanticAI's
  typed tool contracts — but the mechanism is entirely Sonder's own.
- **Sonder grounding:** `tool_capabilities.py` covers 15 of ~184 tools and
  leads its own report with the coverage fraction because "no drift"
  previously meant "no drift in the part I looked at"; `validate_shadow`
  and the AST `dispatch_names()` machinery already exist.
- **Next step:** batch-author descriptors for the ~40 highest-risk tools
  first (everything `mutation`/`dangerous`/`execution` in
  `command_registry.py`), validated by the existing shadow checks in CI.
- **Acceptance:** described fraction for the direct-MCP surface ≥ 90% on
  risk-classified tools; `validate_shadow` clean; one deliberately wrong
  descriptor is caught by CI (RED proof of the validator).

### R13 — Model-aware multi-PC routing (EXP-013) — adapt

- **Borrowed from:** no single harness — standard load-balancer practice;
  the checklist has no good distributed-inference reference.
- **Sonder grounding:** `ollama_pool.py` routes least-inflight over workers
  with no model catalog: a worker missing the model returns 404, which is
  not failover-eligible (`:22,170-174`), so the whole call fails; pooled
  `_get("/api/ps")` (`server.py:4310`) lets fanout residency checks describe
  the wrong machine; `capacity()` remains single-host.
- **Next step:** smallest safe slice: on pool construction and cooldown
  expiry, probe each worker's `/api/tags` once (bounded, cached), route only
  to workers advertising the requested model, and pin `/api/ps`-based
  residency checks to the worker that will execute. Capacity-math
  integration is a separate later experiment.
- **Acceptance:** integration test with a fake worker lacking the model:
  today's behavior (hard failure) becomes routed-around; residency snapshot
  names the executing worker; zero change for single-host deployments.
- **No-go (additional):** keep the module's own contract — "a completed
  request is never replayed on another host" (`ollama_pool.py:1-7`); no
  active health-probe loops beyond the model-catalog probe.

### R14 — Cancellable, parameterized workflows (EXP-014) — adopt (smallest item)

- **Sonder grounding:** `run_loop` accepts a `cancel_check`
  (`application/workflows/loop.py:57`) that treats a broken checker as
  cancelled — correct fail-closed design — but `workflow_run`
  (`server.py:~13945`) never passes one, so a saved workflow cannot be
  stopped.
- **Next step:** pass the same cancellation source the fleet uses; then (as
  a follow-up) consider simple `{placeholder}` parameter substitution before
  any talk of conditionals.
- **Acceptance:** a running workflow stops within one action of a cancel
  request; broken-checker still cancels (existing semantics preserved,
  proven by test).

### Suggested sequencing

Wave 1 (wiring, lowest risk): **R14, R2, R8-shadow, R9-shadow, R12**.
Wave 2 (small builds on proven seams): **R1, R3, R4, R10**.
Wave 3 (needs Wave-1 evidence): **R5, R6, R7, R11, R13**.

---

## 5. Rejections and deferrals

Recorded so they are not silently re-proposed. Each can be reopened by an
experiment that overturns the stated reason.

| Idea | Source | Verdict | Reason |
|---|---|---|---|
| General graph orchestration engine | LangGraph | **reject** | Sonder's fixed fan-out + fail-closed audit (`fleet_provenance` `TASK_DRIFT` gating) is a deliberate, evidence-gated shape. A general graph engine multiplies control-flow surface that permission gates and provenance validation would have to chase. Borrow checkpoint durability (R7) and interrupts (already exist as `control_flags`), not the engine. |
| Free-form multi-agent conversation | AutoGen, CrewAI crews | **reject** | Unbudgeted token use with no per-message grounding; evades evidence receipts (`RepositoryWorkerResult`, `=== TOOL EVIDENCE ===`). Conflicts with the no-token-budget gap (R4) rather than fixing it. |
| Code-as-action default surface | smolagents | **reject** | Dissolves per-tool risk classification (`permission_modes.risk_of()`), which is the unit Sonder's whole safety model prices. `run_code` already exists behind an `execution`-class gate for the explicit case. |
| Self-editing core memory | Letta/MemGPT | **reject** | Sonder's separation — operator-asserted facts vs outcome-earned lessons, with frozen reward prices and provenance vocabularies — is the product's evidence story. Letting the model rewrite its own standing memory reintroduces exactly the self-grading channel `record_self_graded_outcome` was built to quarantine. |
| Hosted observability/eval platforms | LangSmith, Braintrust | **reject** (hosting) / **adopt** (data model, via R1–R3) | Off-host telemetry violates the privacy boundary that is Sonder's stated reason to exist. No consent-gate carve-out is appropriate for standing telemetry. |
| Adding LlamaIndex/Haystack/Semantic Kernel as dependencies | — | **reject** | Pattern extraction only; the dependency inventory discipline (`docs/DEPENDENCY_INVENTORY.md`) and the no-new-runtime-deps default stand. |
| Vector index (sqlite-vec/FAISS) for lessons | LlamaIndex practice | **defer** | Native dependency; brute-force scan is fine at the current ~10³ corpus. Trigger to reopen: measured `retrieve_with_ids` latency > 100 ms at real corpus size, recorded via R3 traces. |
| DSPy-style prompt compilation | DSPy | **defer** | No metric to compile against until R1 exists; hand-authored prompt gates encode judgments (e.g. `reflection.py`'s vague-word ban) that an optimizer would need those metrics to preserve. |
| Per-step workspace checkpoints for ordinary edits | Cline | **defer** | Selfmod's hash-verified bundles cover the high-risk path; per-step snapshots need a measured cost/benefit (disk, latency) and a real incident to justify. Reopen with evidence from R3 traces of edit-caused losses. |
| Web/OS benchmark environments | BrowserGym, WebArena, OSWorld, AgentBench, GAIA, tau-bench (environments) | **reject** (as targets) | Not Sonder's product surface. Their execution-graded design is already Sonder doctrine; tau-bench's pass^k survives as an R1 metric. |
| Docker-per-session runtime | OpenHands | **reject** | `isolated_runner` deliberately keeps containers opt-in, operator-gated, and unavailable to agents (`server.py:12634+`); making them ambient would invert that posture on a machine where Docker may not exist. |

---

## 6. Tiny in-repo improvements shipped with this document

Per the survey's own rules (smallest change directly required by the
matrix, nothing speculative):

1. `tests/test_research_docs.py` — stdlib-only guard that the two schemas in
   this directory stay parseable and internally consistent (every `required`
   key is declared in `properties`; the eval schema keeps `model_grader`
   advisory-capable) and that this document's relative links resolve. It
   exists so a future edit to the schemas cannot silently break the
   contract R1/R3 cite. No runtime code is touched.

## 7. Standing risks of this roadmap

- **Line-number drift.** Citations are anchors for humans, not tests; the
  drift-test culture (`test_advertised_surface_drift.py` et al.) is the
  durable mechanism, and R1/R12 extend it.
- **Wiring dormant code can wake dormant bugs.** R7/R8/R9 deliberately start
  in shadow or drain-only modes; their acceptance criteria require crash and
  RED proofs, not inspection.
- **External summaries age.** §2's external claims are early-2026 snapshots;
  each experiment's first step is re-verifying its upstream reference.
