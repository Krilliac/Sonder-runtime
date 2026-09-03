# Architecture handoff review — 2026-09-02

Status: **design review and proposal** (docs-only; imposes no runtime behavior;
not an evidence record; changes no master-spec checkbox and no ledger entry).
Reviewed revision: `main` at `769a1f7fbbbeaa79b6e1b84ef81ea1edf19f8470`, the same
revision the handoff audited. CI on that revision: `tests` job green,
10,799 passed / 45 skipped (run 33525724101).
Reviewed document: `docs/architecture/SONDER-RUNTIME-ARCHITECTURE-HANDOFF.docx`
on branch `docs/architecture-handoff` (commit `812ef30c`).
Method: three independent read-only sweeps of the code (authority and
permissions; runtime entry points and tool dispatch; evaluation, replay, and
dormant components) plus direct reading of the authority documents. Every
finding names a file and, where useful, a line. Line numbers drift; the symbol
names are the durable anchors.

Per the handoff's instruction, **no runtime code was modified**. This document
is the requested design; implementation waits for approval of the decisions in
section 9.

---

## 0. Answers in one screen

| Question | Answer |
|---|---|
| Is the dominant problem integration and authority unification rather than missing features? | **Yes, with one sharpening.** There are not two runtimes running side by side. There is one live runtime (`server.py`, 25,601 lines) wrapped by a typed shell that owns every entry point and then delegates back to the legacy module for every consequential action. The typed layer holds roughly thirty contract-proven seams that are *constructed* in bootstrap but hold no *authority* over live behavior. Authority itself has one decider (`permission_modes.decide`) whose unattended branch is allow-by-default on every production surface. |
| Which handoff claims are stale or incorrect? | Six need correction before they drive work: trajectory replay is *absent*, not unwired; lesson decay and trace projection *are* wired (as a diagnostic and as an HTTP route) but not authoritative or durable; a golden lane already exists (`eval_harness.py`) but is not a CI gate and lacks three outcome classes; budgets are enforced in several places, just with no shared vocabulary; `file_ops.py` is no longer a root module and containment is already single-sourced in the typed adapter; memory has one store, not two. Details in section 5. |
| Smallest safe first slice? | Narrower than the handoff's three items, and ordered: **A0** make the unattended degrade *visible* (durable, content-free receipts) so its blast radius is measured before it changes; **A1** fail the unattended `ask` closed with the three existing remedies preserved; **B** turn the existing golden lane into a CI gate and add the missing outcome classes plus tool-refusal cases; **C** route the *read-only* workbench family through the typed gateway with durable receipts, legacy handlers forwarding. The mutating file family, capability grants, and fencing tokens are slice 2. |
| Which contracts and tests must remain unchanged? | Every external protocol shape (OpenAI-compatible HTTP, MCP 2.x, REPL JSON Lines, Flutter wire), every containment test for `file_ops`/`workbench`, the root-boundary ratchets, the architecture checker, the error-signal ratchet, outcome-source separation, `plan` mode semantics, the gate-control exemption, durable-authority refusals, compute-fabric no-fallback, and the autopilot stale-lease fence. Exactly four tests pin the behavior slice A1 changes and must be rewritten *deliberately*, not deleted (section 7). |
| First-slice design? | Section 8: data flow, ownership, failure handling, and acceptance tests for A, B, and C, with a decisions list in section 9. |

---

## 1. Repository state confirmed

- `origin/main` is `769a1f7`; nothing landed after the handoff's audit. The working
  tree used for this review is identical to that commit.
- The full suite is green on that revision in CI (10,799 passed, 45 skipped, all
  skips are platform or optional-dependency skips).
- The formal evidence ledger (`docs/architecture/evidence/requirements.jsonl`) has
  204 requirements, all at `implemented_unverified`, none `verified`; the master
  spec has 0 of 204 boxes checked. `REQUIREMENT-AUDIT-NEXT.md` still says "all
  204 are planned", which is now stale in the *other* direction: every row has
  since been raised to `implemented_unverified`. The repository's own conclusion
  stands: the gaps are "production wiring, durable persistence, platform
  activation, end-to-end acceptance", not missing contracts.
- Change velocity since the research documents the handoff leans on
  (`docs/research/harness-landscape.md`, 2026-08-22): 212 commits and 17 merges,
  including the compute fabric (PR #436), the multi-node runtime (PR #431/#432),
  and `eval_harness.py`. Several landscape-era statements are therefore stale;
  they are called out below where the handoff repeats them.

---

## 2. Claim-by-claim verification

Verdicts: **CONFIRMED** (code matches the claim), **PARTIAL** (true in part; the
correction matters for the plan), **STALE** (no longer true at `769a1f7`).

| # | Handoff claim | Verdict | Evidence | Consequence |
|---|---|---|---|---|
| 1 | Two model-call paths | CONFIRMED | Legacy: `server.py` `_make_generate:1384`, `_chat_request:4156`, `_post:4357`, `answer_with_history:5790`. Typed: `application/ports/model_gateway.py`, `adapters/inference/ollama_gateway.py:152`, whose providers *are* `server._serve_target`/`_make_generate` (`adapters/model_bootstrap.py:23-43`, installed by `bootstrap/legacy_model.py:27-31`). Third lane: `adapters/inference/openai_compat_gateway.py`. `server.py:1591` also calls back *into* the typed `ChatService` for offload work (`:1614`), a genuine round trip. | The typed gateway is a facade over the legacy transport, not a parallel transport. Unifying is a wiring change, not a rewrite. |
| 2 | Two memory paths | PARTIAL | One store: root `memory_store.py` is a 13-line `sys.modules` shim to `sonder_runtime/adapters/memory_store.py`. Two *outcome-recording* surfaces: legacy `server.py:5912 record_outcome` (`serve.py` 8 calls, `repl.py` 7 calls) and typed `application/memory/outcome_service.py:31` composed at `bootstrap/app.py:908-910` but with no production caller. Typed memory HTTP/MCP handlers (`interfaces/http/handlers.py:90,115`, `interfaces/mcp/handlers.py`) are referenced only by tests. Root `retriever.py` and `grounded_outcomes.py` remain the live implementations and are imported by a typed adapter (`adapters/learning_health.py:7,10`). | Memory is a "two writers, one store" problem, smaller than the handoff implies. Not part of the first slice. |
| 3 | Two tool-dispatch paths | CONFIRMED | Live: 207 `@mcp.tool()` decorators in `server.py`, 206 listed by `docs/architecture/generated/runtime-reference.md:9`; model-facing dispatcher `server.py:16861 _agent_dispatch`. Typed: `bootstrap/native_mcp.py` (opt-in `python -m sonder_runtime mcp --native`), 50 descriptors, dispatched to `adapters/tool_executor.py:20 ToolExecutorAdapter` (a 274-line `if call.tool ==` chain). `ToolService` (`application/execution/tool_service.py:45`) and `ToolGateway` (`application/tools/gateway_contract.py`) are composed **nowhere** on a live path; `bootstrap/app.py:1043` wires `ToolExecutorAdapter()` directly and `adapters/runtime_container.py:64-65` says its tool graph is "intentionally inert". | The documented gateway pipeline (schema, scope, permission, approval, deadline, invoke, redact, receipt) exists and is tested but governs nothing. Slice C makes it govern one family. |
| 4 | `permission_modes.py` documents that unattended `ask` degrades to `allow` | CONFIRMED, and implemented on every production gate | Docstring `permission_modes.py:40-49`; `ASK_CAVEAT` `:238-243`; enforcement comment `:263-267`; implementation `:844-886` (`source="non-interactive"`). Surfaces passing `interactive=False`: HTTP `interfaces/http/serve.py:2206`, legacy MCP `reloadable_mcp.py:64`, native MCP `bootstrap/native_mcp.py:598` (compute tools only), agent/workbench/autopilot `server.py:16782`, loop `server.py:13468`, `control_command` `server.py:2710`. REPL computes `interactive` from `stdin.isatty()` (`interfaces/repl/repl.py:316-339,425,433`). Live check on this revision: `manual` + unattended gives `allow` for `file_write`, `run_code`, `workspace_run`, `task_create`. | This is the crux and it is exactly as the handoff says. Autopilot and Fleet have no gate of their own; they inherit this one through `server._agent_impl` (`master_orchestrator.py:796`). |
| 5 | No scoped capability grant validated uniformly | CONFIRMED | Closest artifact: `ToolScope` (`gateway_contract.py:26-40`: `principal_id`, `workspace_roots`, `allowed_effects`) plus `ToolGatewayRequest.approval_token/session_id/project_id/execution_world`. Missing: source, provider, grant expiry, nonce, call/token/byte budgets; `execution_world` is validated only as "must be text" (`:78`). Model gateway has no authorization at all. Compute dispatcher binds `worker_id/idempotency_key/request_sha256` (`application/compute_fabric/jobs.py:428-434`): integrity binding, not authority. `tool_capabilities.py` is shadow-only by its own header; `tool_contract.py` is a drift detector. | Grants are a slice-2 build. The seams to hang them on already exist (`ToolScope`, `ResourcePolicy.Decision` with `ALLOW_ONCE/SESSION_GRANT/PROJECT_GRANT/ATTENDED_ONLY`, `StartupAuthoritySnapshot`). |
| 6 | Fencing tokens needed across tools, jobs, fleets, remote compute | CONFIRMED | Leases exist and are tested: autopilot (`adapters/persistence/autopilot_store.py:44,76-79,399-443`; adversarial `tests/test_autopilot_stale_lease.py`), selfmod (`selfmod.py:70,88,114,1598-1685`), launcher (`sonder_launcher.py:786-800`), fleet owner heartbeat (`adapters/persistence/fleet_store.py:477-591`), workflow `revision` CAS (`application/workflows/restart_recovery.py:34-41,143-150`). No monotonic fencing token anywhere. Tools, `durable_locks.py` (advisory, `:29-31`), `isolated_runner.py`, `application/agents`, and compute jobs carry none. The autopilot fence protects the run record between steps (`autopilot_controller.py:606-608,644-646`), not a tool call already in flight. | Correct diagnosis. Slice 2, after receipts exist to carry the token. |
| 7 | Receipts are a redacted output log, not forensic evidence | PARTIAL | `ToolReceipt` (`gateway_contract.py:133-152`) has argument/result digests, `execution_world` (pass-through, unverified), `requester_id`, `effects`. `policy_match` and `model` are declared and **never assigned**. No terminal-state field; `Cancelled`/`DeadlineExceeded` raise before any receipt exists (`:264-269`). Default sink is in-memory (`application/tools/facade.py:112-123`). A durable hash-chained sink exists (`adapters/persistence/tool_audit.py:41`) but `ToolApplicationFacade.compose` (`facade.py:175-199`) has no `audit` parameter and the composition root never passes one; the audit record also drops `execution_world`, digests, and `effects`. Compute `RemoteJobReceipt` (`jobs.py:428-470`) is the stronger one (node identity, request digest, validated state). | Slice C wires the durable sink and adds a terminal-state field; grant identity is slice 2. |
| 8 | Remote or autonomous effects must not silently fall back to host execution | CONFIRMED as already satisfied for compute | `domain/compute_fabric.py:316-318` defaults `allow_local_fallback=False`; `application/compute_fabric/service.py:258-268` re-runs placement only on explicit opt-in; `docs/runbooks/compute-fabric.md:203-209`; pinned by `tests/test_compute_placement_service.py:207-217`, `tests/test_native_mcp.py:212,234`. Fleet and Autopilot have no remote world to fall back from. | Keep as an acceptance criterion; it costs nothing because it already holds. |
| 9 | Trajectory replay "can compare alternate models, prompts, routes, or compaction against real trajectories" | STALE (absent, not unwired) | `application/evaluation/trajectory_replay.py` compares digests of two recorded runs (`replay_trajectory:177`, `compare_trajectories:196`); `eval_harness.py:293 ReplayProvider` substitutes *model responses* from a cassette keyed by (scenario, call index); tool results are **never** substituted, grading really executes code (`eval_harness.py:492 grounding.run_code`). Session replay (`application/session/replay.py:115`) reconstructs recorded state and never re-invokes a model. No tool-result recorder exists anywhere. | "Wire trajectory replay" is a build (tool-result capture + substitution), not a wiring task. Defer behind slice B; use the cassette lane for model-side counterfactuals now. |
| 10 | Section-budgeted context planner is unwired | CONFIRMED | `application/context_planner.py:79 plan()` fails closed on overflow and does not evict (`:77`, eviction is `context_priority.py`). `ContextPlanningFacade` is constructed (`bootstrap/app.py:372`) but its only production read is `last_good()` at `app.py:833`, which is `None` because `assemble()` has no production caller. Live builders: `server.py:1852 _build_system` and `orchestrator.py:369 build_prompt`. | Shadow plan is right (handoff Priority 1). Not in the first slice. |
| 11 | Lesson decay / contradiction detection is unwired | PARTIAL | Wired as a *diagnostic* and a nightly pruner: `memory_quality.py:171,260` (comment `:265` "as a diagnostic"), exposed via `server.py:7709 memory_quality_report`, `:7722 memory_quality_repair`, REPL `repl.py:2347-2349`, HTTP `serve.py:2334-2336`; `lesson_pruner.prune` runs from `scripts/nightly_self_improve.py:197`. Absent from retrieval ranking: `retriever.py` has no decay/stale/contradiction term (ranking is RRF `:120-133`, MMR `:641`, usage boost `:506`, quarantine `:390`). | The handoff's treatment ("shadow rank changes") is right; the label "unwired" is not. |
| 12 | Outbox / queued-action ledgers are dormant | CONFIRMED | Queued actions: no producer, one status read (`bootstrap/app.py:814-829`), by design per `docs/architecture/queued-action-lifecycle.md`. Outbox: tables live in five schemas, `OutboxDispatcher` (`adapters/persistence/sqlite/outbox.py:93`) has zero production instantiations, and the live outcome write (`server.py:3399`) bypasses the only writer path. | Handoff's "wire one real path or retire" stands. Not in the first slice. |
| 13 | Trace projection is unwired | STALE | It is wired: `GET /v1/observability/trace` (`serve.py:599,4016`, `interfaces/http/facades/observability.py:78-92`, `adapters/local_observability.py:434`). It is **not persisted**: source is an in-memory `deque` (`local_observability.py:224`, default 256 events). | The right ask is "persist", not "wire". |
| 14 | Evaluation history only prints | PARTIAL | Identity-pinned durable store exists (`adapters/evaluation_history_store.py`, five-tuple identity `:63`). `eval_harness.py:967` and `eval_models.py` write it behind `--record-history`; `eval_solver.py`, `eval_duel.py`, `eval_retrieval.py` print only. Read side is live (`server.py:15140 evaluation_history_status`). | Make recording the default where a real digest exists; three CLIs still need the write path. |
| 15 | No single golden regression lane | PARTIAL | `eval_harness.py` (post-landscape, 2026-08-22) is a golden lane: versioned suite contract with `suite_hash` (`:249`), immutable digest-bound run records (`:646`), replay cassettes, four never-merged outcome classes (`pass/fail/error/timeout`, `:453,481,732`), checked-in baseline ratchet (`eval_scenarios/eval_baseline.json`), `verify-replay` via `compare_trajectories`. Gaps: one suite (`smoke_python`), one `kind` (`python_function`), no CI invocation (`.github/workflows` never calls it; only its unit tests run), no `pass@k`, no run-A-vs-run-B diff, no `verifier_unavailable`/`unknown`/`abandoned` outcomes (`verifiers.VerifierUnavailable` exists at `verifiers.py:45` but the harness never routes it). Second lane `application/evaluation/reproducible.py` + `case_manifest.py` + `scripts/run_reproducible_eval.py` has a richer vocabulary but is offline-fixture only. | The lane is a hardening task, not a build. Slice B. |
| 16 | Budgets are not enforcement points | PARTIAL | Enforced: agent `max_steps` (default 6, cap 20, `server.py:19700`), selfmod tool-call and runtime budgets (`server.py:2330-2332`), hosted agent output budget 65,536 tokens (`server.py:19481-19485`, raises `ModelCallError("budget")`), deadlines, concurrency admission (`operations/admission_gate.py`), sandbox limits (`isolated_runner.py:75,557,663`). Unwired: child-agent `SubagentBudget` (`application/ports/subagents.py:45`). Shadow: role budgets (`domain/agents/roles.py:23`). Exhaustion is a distinct terminal state only in fanout (`fanout_store.FAILURE_CLASSES` `budget_exhausted`). | "Decentralized, no shared vocabulary" is the accurate statement. |
| 17 | Standard terminal reasons are needed | CONFIRMED | No `TerminalReason` anywhere; at least seven disjoint vocabularies (`fanout_store.py:57`, `loop_contract.py:8,17`, `ports/jobs.py:29`, `workflows/restart_recovery.py:19`, `operations/startup_reconciliation.py:94`, `reproducible.py:49,58`, `sonder_launcher.py:77`). Of the handoff's nine tokens only `budget_exhausted` and `execution_uncertain` exist (fanout only). | Introduce the vocabulary where slice C needs it (tool receipts) and grow it outward. |
| 18 | "Keep containment and destructive-operation logic in `file_ops.py`" | STALE path, correct intent | Root `file_ops.py` no longer exists (`migration-inventory.json` still lists it as a root module; that baseline is dated 2026-08-21). Containment is single-sourced in the typed adapter `sonder_runtime/adapters/filesystem/file_ops.py` (`resolve_path:615`, `resolve_mutation_path:635`, `_require_mutation_access:317`, `_require_safe_recursive_delete:536`, `_delete_tree_guarded:589`) and `workbench.py:85 _resolve`; `server.py:110,143` import these same modules. | The legacy handlers already delegate the guards. Migration is about the handler-level concerns (authorization tokens, recording, activity, outcome attribution, rendering), not about moving guards. |
| 19 | Strengths: unknown ≠ failed; populations separated; crash uncertainty explicit; dependency direction | CONFIRMED | `domain/memory/rules.py:35-38,88-116` (five outcome sources, never averaged), `migrations/memory/0002_outcomes_source.py` (NOT NULL, backfill to `unknown`), `calibration.py:55` (`MIN_SAMPLE=20`, fails closed), `grounded_outcomes.py:141,185,209` (infrastructure error ≠ reward −1); `command_recovery.py` (`uncertain` never redispatched), `served_action_receipts.py:42` (`started/completed/uncertain`), `fanout_store.py:510,745,806` (`execution_uncertain`), `autopilot_controller.py:267-273`; `scripts/check_architecture.py` in CI. | Preserve. Note the one hole: the chat/agent turn has no uncertainty ledger for tool calls (a crash mid-tool leaves no `uncertain` receipt). |
| 20 | Multiple places decide persistence, activity, completion | CONFIRMED with a concrete duplicate | One HTTP chat turn (`serve.py:5423-5570`) runs `_record_chat`, `activity_tracker.response_span`, `server.answer_with_history` (which itself records direct tools, activity, grounded outcomes, and calls `server.py:5760 _capture_durable_session_turn`), **and** `serve.py:5555 _capture_live_session_turn`: the typed `SessionCaptureService` is invoked twice for the same turn from two modules. Three storage roots: `server.py:1157 _open_db`, `adapters/unit_of_work.py:32`, `adapters/persistence/session_repository.py`. | This is the strongest argument for the handoff's Vertical slice 2 (one turn service). It is not the first slice. |

---

## 3. Behavior classification

Vocabulary used throughout, so a reader cannot mistake one for another:

- **implemented**: code and focused tests exist.
- **wired**: a live entry surface reaches it with authority over the result.
- **shadow**: runs beside a live path, or is constructed beside it, without authority.
- **experimental**: reachable by explicit operator opt-in, no default effect, not a promotion gate.
- **proposed**: described in a document, not in code.
- **unsupported**: explicitly out of contract or refused by design.

| Component | Class | Anchor |
|---|---|---|
| Legacy permission decider and its unattended `ask`→`allow` degrade | implemented + wired (all surfaces) | `permission_modes.py:844-886` |
| Typed `ToolGateway` pipeline (schema→scope→permission→approval→invoke→redact→receipt) | implemented, **not wired** (inert graph) | `application/tools/gateway_contract.py:211-262`; `adapters/runtime_container.py:64-76` |
| `ResourcePolicy` with `ALLOW_ONCE/SESSION_GRANT/PROJECT_GRANT/SANDBOX_ONLY/ATTENDED_ONLY` and `StartupAuthoritySnapshot` | implemented, not wired | `application/tools/resource_policy.py:28-35,120-150` |
| Durable hash-chained tool audit | implemented, not wired | `adapters/persistence/tool_audit.py:41` |
| Native MCP typed catalog (50 tools) | implemented + **experimental** (opt-in `--native`) | `bootstrap/native_mcp.py:443` |
| Legacy MCP catalog (206 tools) | implemented + wired (default) | `server.py:3829`, `server.py:25588` |
| Typed `ChatService` | implemented; wired only for `/a2a`; HTTP chat uses legacy `answer_with_history` | `serve.py:4834`, `serve.py:2943` |
| `SessionCaptureService` (append-only, hash-chained session log) | implemented + wired (twice per HTTP turn) | `serve.py:5555`, `server.py:5760` |
| Compute fabric (bounded remote jobs, no local fallback) | implemented + wired | `application/compute_fabric/service.py:258-268` |
| Autopilot lease + stale-owner fence | implemented + wired + adversarially tested | `tests/test_autopilot_stale_lease.py` |
| Fencing tokens for in-flight effects | proposed | none |
| Capability grants | proposed | none (partial seams in `ToolScope`) |
| Golden eval lane `eval_harness.py` with baseline ratchet | implemented; **experimental** as a gate (no CI invocation); live `ollama:` provider untested by design | `eval_harness.py:761,1125-1195`; `docs/evals/README.md` |
| Reproducible evaluation lane + case manifest | implemented, offline fixtures only | `application/evaluation/reproducible.py`, `case_manifest.py` |
| Evaluation history store | implemented + wired (read); write opt-in for two of five CLIs | `adapters/evaluation_history_store.py` |
| Counterfactual trajectory replay with side-effect substitution | proposed (absent) | none |
| Model-response cassette replay | implemented + wired (eval only) | `eval_harness.py:293` |
| Section-budgeted context planner | implemented, shadow-constructed, no caller | `bootstrap/app.py:372,833` |
| Lesson decay / contradiction detection | implemented + wired as diagnostic and nightly pruner; **not** in ranking | `memory_quality.py:171,260`; `retriever.py` |
| Transactional outbox | schema wired; writer reachable only through an uncalled facade; dispatcher never constructed | `adapters/persistence/sqlite/outbox.py:93`; `server.py:3399` |
| Queued-action ledger | implemented, unwired by design | `docs/architecture/queued-action-lifecycle.md` |
| Trace projection | implemented + wired (`/v1/observability/trace`); not durable | `adapters/local_observability.py:224` |
| Outcome population separation | implemented + wired, migration-enforced | `domain/memory/rules.py`, `migrations/memory/0002_outcomes_source.py` |
| Crash-uncertainty ledgers (launcher commands, HTTP served actions, fanout, startup reconciliation) | implemented + wired | `command_recovery.py`, `served_action_receipts.py`, `fanout_store.py`, `startup_reconciliation.py` |
| Crash-uncertainty ledger for chat/agent tool calls | proposed (absent) | none |
| Child-agent budgets (`SubagentBudget`) | implemented, not wired | `application/ports/subagents.py:45` |
| Role budgets / `RoleBudgetBook` | implemented, shadow | `model_gateway/health_and_roles.py:114` |
| Unified terminal-reason vocabulary | proposed | none (closest: `fanout_store.FAILURE_CLASSES`) |
| `permission_modes.decide(non_degrading=...)` | **unsupported** (dead parameter; documented as live) | `permission_modes.py:756,764-782` |
| `ToolReceipt.policy_match`, `ToolReceipt.model` | unsupported (declared, never assigned) | `gateway_contract.py:149,152` |
| Unsafe lab mode | implemented + wired, explicitly not a sandbox | `SECURITY.md` |

---

## 4. Question 1: is the dominant problem integration and authority unification?

Yes. Three verified facts carry the argument:

1. **Every consequential action still runs in the legacy module.** All launchers
   enter through `python -m sonder_runtime`, but the HTTP chat turn ends in
   `server.answer_with_history`, the default MCP surface serves the 206-tool
   legacy catalog, the REPL calls `server.sonder`, and the desktop/mobile client
   posts to the same HTTP route. The typed package reaches the legacy module
   through one sanctioned import (`bootstrap/legacy_root.py`) and two proxy
   objects with 162 and 77 call sites respectively (`serve.py:134-152`,
   `repl.py:186`). The boundary ratchets (`tests/test_wp1_root_server_boundary.py`)
   hold the *import* line; they do not move the *authority* line.
2. **The typed seams are proven but inert.** `REQUIREMENT-AUDIT-NEXT.md` classifies
   162 of 163 requirements as `PROVEN-CONTRACT` and then says, correctly, that a
   contract slice is not an end-to-end proof. The composition root builds a
   `ToolApplicationFacade` over an empty registry and says so in a comment. The
   tool pipeline the master spec calls "one gateway" (TOOL-001) governs no live
   call.
3. **Authority is unified already, in the wrong direction.** There is exactly one
   decider, and its unattended branch resolves `ask` to `allow` on every
   protocol surface. That is a coherent policy, honestly documented, and it is
   the policy the handoff wants reversed. Reversing it is a small code change
   with a large behavioral blast radius, which is why the first slice measures
   before it flips (section 8.1).

What is genuinely *missing* rather than unwired is short and each item is a
prerequisite for measuring the migration, so "safety and measurement first"
holds: a tool-result recorder for counterfactual replay, a shared terminal-reason
vocabulary, `pass@k` reporting, fencing tokens, and scoped grants. None of these
is large. None should be built before the receipts and the evaluation gate that
would prove them exist.

One disagreement of emphasis: the handoff frames the risk as "multiple places
that can own the same behavior". The verified picture is "one place owns each
behavior and it is the legacy module, while the typed layer duplicates the
*recording* of that behavior" (the doubled session capture on one HTTP turn is
the clearest instance). The remedy is the same, but the sequencing differs:
move authority for one family, then delete the duplicate recorder, rather than
first building a typed owner and then hoping the legacy one withers.

---

## 5. Question 2: stale or incorrect claims

Corrections a reader should apply to the handoff before planning from it:

1. **Trajectory replay is absent, not dormant.** Nothing records tool results, and
   nothing substitutes them. What exists is a digest comparator between two
   recorded runs and a model-response cassette for the evaluation harness. The
   handoff's "recommended treatment" (wire it into ordinary evaluation runs with
   substituted side effects) is a build with a new persistence format.
2. **Lesson decay and contradiction detection are wired**, as an operator
   diagnostic (`memory_quality_report` on REPL, HTTP, and MCP) and as a nightly
   pruner. They have no authority over retrieval ranking. "Shadow the ranking
   change" remains the right next step.
3. **Trace projection is wired** behind `/v1/observability/trace`. It is not
   persisted; the treatment should read "persist with bounded retention", not
   "wire".
4. **A golden lane exists.** `eval_harness.py` has the versioned contract, the
   immutable run record, the never-merged outcome classes, the checked-in
   ratchet, and the replay comparator the handoff asks for. It does not run in
   CI as a gate, has one suite of one kind, and lacks `verifier_unavailable`,
   `unknown`, `abandoned`, `pass@k`, and a run-A-vs-run-B diff. The handoff's
   Priority 0 for evaluation is therefore "harden and gate", roughly a quarter
   of the work its text implies.
5. **Budgets are enforced in several places.** Steps, selfmod tool calls and
   runtime, hosted output tokens, deadlines, concurrency admission, and sandbox
   resources all have hard stops. What is missing is a shared vocabulary and
   the child-agent budgets, and exhaustion is a distinct terminal state only in
   fanout.
6. **`file_ops.py` is not a root module** and containment is already
   single-sourced in `sonder_runtime/adapters/filesystem/file_ops.py`, which the
   legacy handlers import. The workbench migration does not move guards.
7. **Memory has one store.** The duplication is in outcome *recording* surfaces
   and in two root modules (`retriever.py`, `grounded_outcomes.py`) that a typed
   adapter still imports.
8. **The evidence ledger is not "all planned"** any more; it is all
   `implemented_unverified`. The conclusion (nothing `verified`) is unchanged.
9. **The docstring's "three of the five call sites pass `interactive=False`"**
   undercounts. Seven production gates do (HTTP, legacy MCP, native MCP compute
   tools, agent dispatch, loop, `control_command`, plus the REPL when stdin is
   not a TTY).
10. **`decide(non_degrading=...)`** is documented in the handoff's source module
    as the live protection for `/selfmod deploy`. The parameter is never read
    and never passed; the real protection is a separate check in
    `server.py:2573-2591`. This is a defect, not a design fact (section 11).

Claims verified as accurate and load-bearing: the unattended degrade (4), the
absence of grants (5) and fencing tokens (6), receipts as non-forensic (7), the
already-satisfied no-local-fallback rule for compute (8), the dormant context
planner (10), the dormant outbox and queued actions (12), the missing terminal
vocabulary (17), the strengths (19), and the multiple deciders per turn (20).

---

## 6. Question 3: the smallest safe first slice

The handoff proposes three items in one slice. Two of the three are right; the
third (a full workbench tool-family migration) is too large for a first cut
because the mutating file tools carry the legacy token/approval model that the
typed surface deliberately does not accept (`WP8-NATIVE-MCP-MIGRATION.md`: "do
not accept legacy authentication tokens on the native surface"). Reconciling
those two authorization models is a design decision (section 9), not a
refactor.

The proposed first slice, each part independently shippable and reversible:

| Part | What | Why first | Size |
|---|---|---|---|
| **A0** Unattended-degrade receipts | Every `Decision(source="non-interactive")` on every surface emits one durable, content-free event (tool, surface, mode, risk, correlation id) to `operations.db` through the existing event sink, and `/permissions` shows the count since restart. No behavior change. | The floor test `test_manual_refuses_nothing_the_mode_did_not_refuse_before` exists because "Sonder stopped working" is the failure mode of a fail-closed change. Measuring the degrade on real usage for a week tells Nathan exactly which tools would be refused and from which surfaces before anything is refused. This is the repository's own "RED proof, then shadow, then cutover" discipline. | ~150 lines + tests |
| **A1** Fail the unattended `ask` closed | In `decide()`, an unattended `ask` returns `DENY` with `source="unattended"` and a reason naming the three remedies that already exist (explicit `allow` rule, `auto` mode, answering at the console). `plan`, `auto`, explicit rules, `GATE_CONTROL_TOOLS`, and `DURABLE_AUTHORITY_TOOLS` semantics are unchanged. Refusal shapes per surface are unchanged. | The handoff's Priority 0. The change is ~20 lines in one function; the work is in the four tests that pin today's behavior, the display copy, and the client blurb. | ~40 lines + 4 rewritten tests + copy |
| **B** Golden lane as a gate | Run `eval_harness.py run --suite smoke_python --check-baseline` as a step of the `tests` job (offline, cassette-backed, seconds). Add outcome classes `verifier_unavailable` and `unknown` to the harness. Add a `tool_policy` scenario kind that replays a recorded tool proposal through the real permission gate and expects refusal. Add `compare --run A --run B` producing machine-readable regressed case ids. Record `k` raw trial outcomes and report `pass@1` and `pass@k` separately. | Everything after this slice must be measured against something that fails when it should. The lane exists; it needs a RED proof in CI. | ~300 lines + fixtures |
| **C** Read-only workbench family through the typed gateway | Compose `ToolApplicationFacade` in `bootstrap/app.py` for `directory_tree`, `file_find`, `file_read`, `file_read_range`, `text_search`, `script_search`, `program_search`. Permission port = an adapter over `permission_policy.decide_for_caller`. Receipt sink = the durable audit repository, extended with a terminal state. Native MCP dispatches this family through the gateway; the seven legacy `server.py` handlers become forwards that render the legacy string. | Reads carry no destructive risk, no legacy approval tokens, and already have parity tests on both surfaces. Proving receipts, audit chain, context identity, and parity on reads makes the mutating family a mechanical second slice. | ~400 lines + parity tests |

Excluded from the first slice, deliberately: the mutating file family (needs
the token-model decision), capability grants (needs receipts to carry them),
fencing tokens (needs receipts to carry them), the conversation-turn service
(Vertical slice 2 in the handoff, correct but large), context-planner shadowing,
lesson-decay shadowing, outbox wiring, and any change to routing or stopping.

---

## 7. Question 4: contracts and tests that must not change, and the ones that must

### Must remain unchanged (and green)

| Contract | Pinned by |
|---|---|
| OpenAI-compatible HTTP request/response shapes and policy ordering | `tests/test_wp8_openai_compatibility.py`, `tests/test_api004_openai_integration.py`, `tests/test_serve_*.py`, `tests/test_http_serve_policy.py`, `tests/test_wp1_http_model_facade.py` |
| MCP 2.x transport, legacy catalog, native catalog, alias normalization | `tests/test_wp8_mcp_compatibility.py`, `tests/test_native_mcp.py`, `tests/test_mcp_stdio_transport.py`, `tests/test_reloadable_mcp.py`, `tests/test_api003_*.py` |
| REPL facade, JSON Lines output, replay | `tests/test_wp1_repl_facade.py`, `tests/test_repl_*.py` |
| Flutter/mobile wire parity | `tests/test_mobile_parity_wire.py`, `tests/test_remaining_client_schema.py` |
| Tool contract agreement between surfaces | `tests/test_tool_contract_conformance.py`, `tests/test_permission_gate_coverage.py`, `tests/test_command_catalog*.py`, `tests/test_agent_help_dispatch_drift.py` |
| File and workbench containment, sensitive-path refusal, degradation | `tests/test_file_ops_paths_boundary.py`, `tests/test_file_ops_containment_degradation.py`, `tests/test_file_ops_sensitive_read.py`, `tests/test_workbench_paths_boundary.py`, `tests/test_workbench.py`, `tests/test_harness_root_confinement.py`, `tests/test_remaining_execution_containment.py` |
| Root-boundary ratchets (only `legacy_root.py` imports `server`) | `tests/test_wp1_root_server_boundary.py`, `tests/test_wp1_legacy_root_boundary.py`, `tests/test_lazy_legacy_model_boundary.py`, `tests/production/test_architecture.py`, `scripts/check_architecture.py` |
| Error-signal ratchet (no new literal `ERROR:` returns) | `scripts/check_error_signals.py`, `tests/production/test_error_signal_ratchet.py` |
| Outcome-source separation and calibration populations | `tests/test_outcome_source.py`, `tests/test_calibration.py`, `tests/test_grounded_outcome*.py` |
| `plan` denies every non-read on every surface; `plan` cannot trap the client or the console | `tests/test_permission_gate_dispatch.py::test_plan_*`, `tests/test_plan_mode_read_only_surface.py` |
| Gate-control exemption has one definition; the agent path gets none | `test_the_gate_control_exemption_has_one_definition`, `test_the_agent_path_gets_no_gate_control_exemption` |
| Durable-authority tools are refused unattended; rule `deny` is never softened | `tests/test_permission_durable_authority.py`, `test_rule_deny_is_immune_to_the_non_interactive_degrade`, `test_degraded_policy_cannot_relax_noninteractive_tool_gate` |
| Console never prompts for a read; the prompt defaults to no; a missing terminal is a no | `test_console_never_prompts_for_a_read`, `test_the_prompt_defaults_to_no`, `test_a_missing_terminal_is_a_no_not_a_yes` |
| Compute fabric: no local fallback, no non-idempotent retry | `tests/test_compute_placement_service.py`, `tests/test_native_mcp.py` |
| Autopilot stale-lease fence | `tests/test_autopilot_stale_lease.py` |
| Eval harness outcome classes never merged; baseline pins `suite_hash` | `tests/test_eval_harness.py`, `tests/test_eval_harness_e2e.py` |
| Evaluation history identity | `tests/test_eval_history.py`, `tests/test_evaluation_history_service.py` |
| Session capture, replay, hash chain | `tests/production/test_application_session_wiring.py`, `tests/test_session_replay.py`, `tests/test_remaining_session_durable_replay.py` |
| Document authority and generated catalogs | `tests/test_document_authority.py`, `tests/test_remaining_doc_001_005.py`, `scripts/generate_documentation_catalogs.py --check` |

### Must change deliberately in slice A1 (rewrite, never delete)

| Test | Today | After |
|---|---|---|
| `tests/test_permission_modes.py::test_ask_degrades_to_allow_when_not_interactive` (line 423) | `manual` + unattended `file_write` is `allow` | is `deny`, `source == "unattended"`, reason names all three remedies |
| `tests/test_permission_gate_dispatch.py::test_manual_refuses_nothing_the_mode_did_not_refuse_before` (line 457) | refused set == rule-denied ∪ durable-authority | refused set == rule-denied ∪ durable-authority ∪ {catalog tools of risk `ask`/`mutation`/`execution`/`dangerous` without an explicit `allow` rule}; keep the assertion that the durable-authority set is not agent-dispatchable |
| `tests/test_permission_gate_dispatch.py::test_manual_allows_every_risk_class_on_the_agent_path` (line 509) | `status`, `task_plan`, `file_write`, `run_code` all pass | becomes two tests: `safe` passes; `ask`/`mutation`/`execution` refused with the unattended reason; and a new `auto` counterpart where all but `dangerous` pass |
| `tests/test_permission_gate_dispatch.py::test_manual_leaves_the_direct_mcp_surface_alone` (line 517) | `task_create` (risk `ask`) succeeds over MCP in `manual` | succeeds in `auto` or with an `allow` rule; refused in `manual` with the unattended reason in the `ToolError` text |

Display and copy that change with it: `permission_modes.ASK_CAVEAT` and
`MODE_BLURBS` (shipped to the Flutter client through `server.permission_mode_data`
at `server.py:9745` and rendered by `app/lib/api.dart:654`), the `/permissions`
suffix `"-> ask (%s) / allow (non-interactive)"` at `permission_rules.py:410`,
the source→text map at `server.py:7665`, `docs/wiki/20-terminal-ui-conventions.md:16`,
and the docstrings of `reloadable_mcp._refuse_if_gated` and
`tests/test_permission_gate_http.py`. `tests/test_permission_policy_display.py`
pins the copy and must be updated in the same change.

---

## 8. Question 5: first-slice design

Ownership rule applied throughout (the handoff's, and the master spec's):
application owns lifecycle and use-case ordering; interfaces translate protocol;
tools own authorization and execution; adapters own I/O; domain stays pure;
bootstrap composes. Where the legacy module is still the authority, the typed
layer adapts *to* it behind a port rather than growing a second copy of the
precedence table (the `Decision` docstring already warns against that second
copy).

### 8.1 Slice A: unattended authority

**A0, visibility first.**

Data flow:

```text
surface -> permission_policy.decide_for_caller(tool, interactive=False, ...)
        -> Decision(action, mode, risk, reason, tool, source)
        -> [new] if source == "non-interactive": events.emit(
               kind="permission.unattended_degrade",
               payload={tool, surface, mode, risk, correlation_id})   # content-free
        -> existing per-surface behavior (unchanged)
```

- Emitter: one function in `sonder_runtime/adapters/security/permission_policy.py`
  (the provider every typed surface already calls) so no surface grows its own
  copy; the legacy call sites in `server.py` (`:16782`, `:13468`, `:2710`) and
  `reloadable_mcp.py:64` call the same provider method. `surface` is the
  `OperationContext.source` value where a context exists (`http`, `mcp`,
  `repl`, `worker`, `system`) and a fixed label at the three legacy sites.
- Sink: the existing `Application.events` (`LocalObservabilitySink` over
  `OperationsEventSink`); durable authority is `operations.db`. The payload holds
  no path, argument, or prompt text.
- Read side: `/permissions` and `permission_policy()` gain one line, "unattended
  degrades since start: N (last: tool, surface)", from the process-local
  counter in the sink; the durable rows are queryable through the existing
  operations event surface.
- Failure handling: emission is best-effort and never changes the decision; the
  sink already swallows storage errors and says so (`local_observability.py:268-270`).
- Acceptance: (1) a test that a `manual` unattended `file_write` produces exactly
  one event with the five fields and no other keys; (2) the same for each of
  the seven surfaces via their existing gate tests; (3) a redaction test that a
  tool argument containing a secret never appears in the event; (4) the full
  suite unchanged in count except the additions; (5) the error-signal ratchet
  unchanged.
- Duration: run for one week of ordinary use, then read the counts before A1.

**A1, fail closed.**

Change in `permission_modes.decide()` step 5, after the `UNCLASSIFIED` and
`DURABLE_AUTHORITY_TOOLS` branches:

```text
if action == ASK and not interactive:
    5a unclassified      -> DENY (unchanged)
    5b durable authority -> DENY (unchanged)
    5c [new] every other risk -> DENY, source="unattended", reason:
       "<tool> needs a decision and nobody is here to make one; write an
        explicit allow rule with /permissions, select auto mode, or run it
        from the console and answer the prompt"
```

Everything above step 5 is unchanged: an explicit `deny` rule still wins, an
explicit `allow` rule still satisfies the ask at step 3, `plan` still denies,
`auto` still allows `safe/ask/mutation/execution` and asks (now: refuses
unattended) for `dangerous`. The live matrix on this revision confirms `auto`
already refuses the five durable-authority tools unattended because they are
`dangerous`-class, so A1 does not need a special case for them.

Consequences per surface (all keep their existing refusal shape):

| Surface | Refusal shape (unchanged) | Note |
|---|---|---|
| Legacy MCP | `ToolError("... refused by the active permission gate: <reason> (mode=..., risk=...)")` | The outer harness (Claude Code or Codex) sees `isError` and the reason; its human can act |
| Native MCP | `{"isError": true, "error": "permission_denied"}` | today only for compute tools; slice C extends it to the read family |
| HTTP slash / task tools | `"refused <label>: <reason> (mode: ...)"` | unchanged |
| Agent, workbench, autopilot, fleet | `"ERROR: HOST POLICY: tool '...' is refused ... not a transient failure: retrying it unchanged will be refused again"` | the loop already treats this as a failed step and tells the model to change course; keep it assigned, not returned as a literal, so the error-signal ratchet is untouched |
| Loop / workflow | `_loop_permission_refusal` | unchanged |
| REPL with a TTY | prompt, default no | unchanged |

Ownership: `permission_modes.py` remains the single decider (a root module by
the compatibility policy; ADR-007 says no new business logic in a shim, and this
is the engine, not a shim). No typed copy of the precedence table is created.

Failure handling: the decider is pure. The only new failure mode is a caller
that previously succeeded now receiving a refusal; every refusal names its
remedy, and the A0 counts tell Nathan in advance which callers those are.

Default-mode question: `DEFAULT_MODE` stays `manual`. With A1, a fresh install
driven only over MCP or HTTP refuses every mutation until the operator either
writes rules or selects `auto`. That is the handoff's intent ("a caller with
nobody available to answer should not receive an implicit approval"). Whether
the first refusal should print a one-time onboarding hint is decision 2 in
section 9.

Acceptance tests for A1:

1. RED proof: the four tests in section 7 fail before the change and pass after
   their deliberate rewrite; no other test changes.
2. Permission matrix across every entry surface (the handoff's own criterion):
   for each of HTTP slash, legacy MCP `call_tool`, native MCP, `_agent_dispatch`,
   the loop, `control_command`, and autopilot via `_agent_impl`: an
   `ask`-class mutation with no rule in `manual` is refused; with an explicit
   `allow` rule it proceeds; in `auto` it proceeds; a `dangerous` tool in `auto`
   is refused; in `plan` everything non-read is refused; a `safe` read is never
   refused; `GATE_CONTROL_TOOLS` remain exempt on the person-facing surfaces and
   not on the agent path.
3. The `unattended` refusal is recorded by the A0 emitter as
   `permission.unattended_refusal` with the same five fields.
4. Every display surface that names the old behavior is updated and pinned:
   `ASK_CAVEAT`, `MODE_BLURBS`, `permission_rules._effective_suffix`, the
   `server.py:7665` source map, the wiki line, the Flutter blurb (via the
   `permission_mode_data` payload test).
5. `scripts/check_error_signals.py` and `scripts/check_architecture.py` exit 0.
6. `scripts/select_regression_tests.py --since main` set green, then the full
   suite green.
7. `tests/test_selfmod_deploy_gate.py` unchanged: `/selfmod deploy` keeps its
   separate protection, and the dead `non_degrading` parameter is removed in
   the same change so the docstring stops describing a mechanism that does not
   exist (section 11).

### 8.2 Slice B: golden evaluation lane as a gate

Data flow (existing pieces named as they are):

```text
eval_scenarios/<suite>.json (sonder.eval-harness.suite/v1, suite_hash)
  -> eval_harness.run_suite(provider=replay | ollama:<model> --live)
       -> per case: solver -> [new] verifier via solve_verified
                 -> status in {pass, fail, error, timeout,
                               [new] verifier_unavailable, unknown}
       -> results.jsonl, traces/<id>.jsonl, summary.json (run/v1, report_id)
       -> sonder.evaluation-trajectory.v1 record
  -> check_baseline(eval_scenarios/eval_baseline.json)   # ratchet, existing
  -> [new] compare(run_a, run_b) -> {regressed: [case ids], reason_codes}
  -> --record-history -> eval-history.jsonl (identity-pinned, existing)
```

Changes:

1. **CI gate.** Add one step to the `tests` job in `.github/workflows/ci.yml`
   (a step, not a job: `tests` is a required-status-check name and must keep its
   id): `python eval_harness.py run --suite smoke_python --check-baseline`, offline
   by default, exit 1 on baseline violation, exit 2 on infrastructure error.
   Upload `eval_runs/` as an artifact alongside `pytest-report.xml`.
2. **Outcome classes.** Route `solver.solve_verified` (which already lets
   `VerifierUnavailable` propagate, `solver.py:188`) instead of `solver.solve`
   for scenarios that declare a verifier; map `VerifierUnavailable` to status
   `verifier_unavailable` and any crash between "started" and "graded" to
   `unknown`. Both are excluded from `pass_rate`'s graded denominator, reported
   separately, and gated by the baseline's `forbid_infra` list, exactly as
   `error`/`timeout` are today. `abandoned` is reserved for live-provider runs
   that hit the wall-clock ceiling after at least one attempt.
3. **Tool-policy scenarios.** New `kind: "tool_policy"`: the case carries a
   recorded tool proposal (name, arguments, mode, surface); the runner sends it
   through the real gate (`server._agent_permission_gate_error` for the agent
   path, `permission_policy.decide_for_caller` for the others) and grades
   `expected: refuse | allow`. No model call, no cassette. Seed cases: `file_delete`
   in every mode (refused by rule), `file_write` unattended in `manual` (refused
   after A1, allowed before: this case is the RED proof for A1), `admin_register`
   unattended in `auto` (refused), `task_list` in `plan` (allowed).
4. **`compare`.** `eval_harness.py compare --run A --run B`: join `results.jsonl`
   by scenario id, classify each as `same | regressed | improved | infra`, emit
   `comparison.json` with regressed ids and reason codes, and reuse
   `compare_trajectories` for step-level divergence when both runs carry
   trajectories. Reuse the shape of `application/evaluation/reproducible.py`
   `RegressionAssessment` so the two lanes converge on one record type.
5. **Trials.** `--trials k` repeats each case, stores every raw outcome, and
   reports `pass@1` (first trial) and `pass@k` (any trial) as separate fields.
   On the replay provider `k` trials are identical by construction; the flag
   exists for `--live` runs and the report labels replay results as
   deterministic.
6. **History by default where honest.** When the provider carries a 64-hex
   content digest, record history unless `--no-record-history`; otherwise keep
   refusing, as today. `eval_solver.py`, `eval_duel.py`, `eval_retrieval.py`
   gain the same opt-in flag through the shared harness.

Ownership: the pure pieces (outcome vocabulary, comparison record, trial
aggregation) go in `sonder_runtime/application/evaluation/` next to
`reproducible.py`; `eval_harness.py` stays the operator CLI and consumes them.
Verifier registry stays `verifiers.py`. Promotion stays with
`promotion_eval.promotion_decision`; the harness never promotes.

Failure handling: unknown `kind` rejected at load (existing); a suite/provider
pair absent from the baseline is a violation (existing); infrastructure
outcomes never count as graded (existing, extended); the CI step's exit code 2
is distinguishable from a regression.

Acceptance tests:

1. RED: a scenario whose check is deliberately broken fails the CI step; a
   scenario whose verifier is deliberately unavailable yields
   `verifier_unavailable` and neither `pass` nor `fail`.
2. `verify-replay` remains `equivalent` across two runs of the cassette.
3. `compare` on a run and a doctored copy names exactly the doctored case.
4. Restart survival: run, kill, rerun; the earlier run directory and history
   record are intact and digest-verified.
5. The tool-policy suite passes on main today with `file_write`-unattended
   expected `allow`, and is flipped to `refuse` in the same change as A1.
6. `docs/evals/README.md` updated; `tests/test_eval_harness*.py` extended for
   each new class and command.

### 8.3 Slice C: the read-only workbench family through the typed gateway

Family: `directory_tree`, `file_find`, `file_read`, `file_read_range`,
`text_search`, `script_search`, `program_search`. All seven are `safe`-class
reads on the live matrix (checked on this revision: `manual` and `plan` both
allow them unattended), present on both the legacy and native catalogs, and
already contained by the shared adapter guards. `workspace_inventory`
(`server.py:11100`) is a legacy-only read with no native descriptor; add it as
an eighth member only once its descriptor exists.

Composition (bootstrap):

```text
bootstrap/app.py
  registry   = InMemoryToolRegistry(descriptors for the seven names,
               taken from native_tool_registry so schemas stay identical)
  policy     = ResourcePolicy()            # existing, fail-closed on no match;
                                           # seeded with allow rules for the
                                           # seven read tools, effect "read"
  permissions= PermissionModesEvaluator()  # [new adapter] calls
               permission_policy.decide_for_caller(tool, interactive=False,
               gate_control_exempt=<from context.source>) and raises Forbidden
               with decision.to_dict() on deny  -> ONE decider, adapted
  approvals  = DenyApprovalGate()          # reads never require approval
  executor   = [new adapter] maps (descriptor, ToolCall, OperationContext,
               execution_class) -> ToolExecutorAdapter.execute(...)  # guards unchanged
  redactor   = PatternOutputRedactor(Redactor().redact)  # existing
  receipts   = ReceiptStore()              # process-local, existing
  audit      = DurableToolAuditRepository(SONDER_HOME/audit/tool-receipts.jsonl,
               limits=ToolAuditLimits(...))                  # existing, now wired
  context_factory = [fixed] carries the real OperationContext (source, auth
               level, roots) instead of the hard-coded source="repl"/auth="local"
               in typed_gateway.default_tool_context
  tools = ToolApplicationFacade.compose(registry, executor, policy=policy,
               approvals=approvals, redactor=redactor, receipts=receipts,
               audit=audit)   # compose() gains the audit parameter that
                              # ToolGateway.from_typed_ports already accepts
```

Data flow for one call:

```text
native MCP execute(name, args)
  -> ToolGatewayRequest(request_id=correlation, tool_name, arguments,
       scope=ToolScope(principal=context.principal_id, roots=context.workspace_roots,
                       allowed_effects={"read"}),
       permission=ToolPermission(effects={"read"}), deadline, cancellation,
       session_id, project_id, execution_world="local")
  -> ToolGateway.execute: control -> schema -> effects⊆scope -> PermissionModesEvaluator
     -> control -> executor (file_ops/workbench guards) -> control -> redact
     -> ToolReceipt(..., [new] terminal="completed"|"failed")
     -> audit.append (hash chain)  -> receipts.record
  -> MCP envelope {output, isError, error, evidence}   # unchanged shape

legacy server.file_read(path, max_bytes, token, approval, extra_roots)
  -> _maybe_live_reload()                              # unchanged
  -> build the same ToolGatewayRequest through a typed factory
     (extra_roots -> ToolScope.workspace_roots only when
      _file_bypass_allowed(token, approval); otherwise refused as today)
  -> application.tools.execute(request)
  -> ReceiptSink adapter [new] performs what the handler did after the call:
     _record_direct_tool, activity_tracker.record_event, _feed_grounded_outcome
  -> _format_file_result(...) or "ERROR: %s"           # unchanged string shape
```

Receipt and terminal state: add `terminal: str` to `ToolReceipt` with values
`completed`, `failed`, `cancelled`, `deadline_exceeded`, `policy_denied`, and
make `ToolGateway.execute` emit a receipt on the `Cancelled`, `DeadlineExceeded`,
and `Forbidden` exits before re-raising. Populate `policy_match` from the
`Decision.source`/reason and `model` with `""` explicitly documented as
"not a model call". Extend the audit record with `execution_world`,
`argument_digest`, `result_digest`, `effects`, `policy_match`, `terminal`. This is
the first terminal-reason vocabulary in the repository; grow it from here
rather than inventing one abstractly.

Ownership after C: `application/tools` owns the pipeline; `adapters/filesystem`
owns I/O and containment (unchanged); `adapters/security/permission_policy`
adapts the single decider; `bootstrap/app.py` composes; `bootstrap/native_mcp.py`
and the seven `server.py` handlers translate. No interface dispatches the seven
names to `file_ops`/`workbench` directly, and a source-level test pins that.

Failure handling:

- Deadline or cancellation at any of the three control points produces a receipt
  with the corresponding terminal state and the surface's existing error text.
- Audit failure (`ToolAuditError`: bound exceeded, unreadable chain, redaction
  failure) blocks the receipt and fails the call closed, as the durable audit
  evidence document already specifies; the operator remedy is rotation, which
  the audit repository does not yet implement (decision 5).
- Registry miss is `InvalidInput` before any I/O.
- Live reload keeps working for the legacy handlers because the forward is
  inside the handler body, after `_maybe_live_reload()`.

Acceptance tests:

1. Parity: for each of the seven tools on a fixture workspace, the native MCP
   result and the legacy forward produce identical redacted output, identical
   refusal for a path outside the roots, identical refusal for a sensitive
   control path, and identical `argument_digest`/`result_digest` in their
   receipts.
2. One durable audit record per call on both surfaces; `verify()` passes; the
   record carries `source` (`mcp` vs `http`/`repl`) taken from the real context,
   proving the `default_tool_context` defect is closed.
3. Cancelled and expired requests produce receipts with `terminal` set; no
   receipt is missing for any exit path.
4. `permission_modes` remains the only decider: a test that flips the mode to
   `plan` and observes both surfaces refuse the reads that `plan` refuses
   (none of the seven; the test proves the evaluator is consulted by asserting
   the `permission.*` event count).
5. Source-level ratchet: no module outside `adapters/tool_executor.py` and the
   filesystem adapters calls `file_ops.read_file`/`find_files`/... for these
   names; the seven `server.py` handlers contain no direct guard call.
6. Generated catalogs (`scripts/generate_documentation_catalogs.py --check`,
   `tests/test_remaining_tool_catalogs.py`) publish the migrated group with a
   changed digest.
7. `tests/test_tool_contract_conformance.py`, all file/workbench containment
   tests, `tests/test_native_mcp.py`, and `tests/test_legacy_tool_executor.py`
   unchanged and green.

### 8.4 Sequencing and touch list

Order: A0 → B (they are independent; B's tool-policy suite gives A1 its RED
proof) → C → A1. Putting A1 last lets C's receipts record the first refusals
with real terminal states, and lets the A0 week finish before anything is
refused.

Files touched (estimate; no code written yet):

| Part | Files |
|---|---|
| A0 | `sonder_runtime/adapters/security/permission_policy.py`, `server.py` (three gate sites), `reloadable_mcp.py`, `permission_rules.py` (display), tests |
| A1 | `permission_modes.py` (one branch, one dead parameter removed), four tests rewritten, `tests/test_permission_policy_display.py`, `permission_rules.py:410`, `server.py:7665`, `app/lib` blurb copy via server payload, `docs/wiki/20-terminal-ui-conventions.md` |
| B | `eval_harness.py`, `sonder_runtime/application/evaluation/` (vocabulary, comparison, trials), `eval_scenarios/` (tool-policy suite, baseline), `.github/workflows/ci.yml` (one step), `docs/evals/README.md`, tests |
| C | `bootstrap/app.py`, `bootstrap/native_mcp.py`, `application/tools/facade.py` (audit parameter), `application/tools/gateway_contract.py` (terminal state, receipt on early exit), `application/tools/typed_gateway.py` (context factory), `adapters/persistence/tool_audit.py` (fields), new `adapters/security/permission_evaluator.py`, new executor adapter, seven handlers in `server.py`, tests |

---

## 9. Decisions Nathan must make before implementation

1. **Accept the narrowed first slice** (A0, B, C, then A1) instead of the
   handoff's three-item slice, with the mutating file family as slice 2.
2. **Default posture after A1.** Keep `DEFAULT_MODE = manual` (fresh MCP/HTTP
   installs refuse every mutation until rules or `auto` are set), or introduce
   a first-refusal onboarding hint. My recommendation: keep `manual`, print the
   hint once per process, no new mode.
3. **Legacy approval tokens on the typed path.** The native surface refuses
   `token`/`approval`/`extra_roots` by design; the legacy handlers must keep
   accepting them. Proposed rule for slice C: the forward maps them into the
   typed request only through the existing `_file_bypass_allowed` and
   `_file_developer_allowed` checks, so no new authority path opens. Slice 2 (the
   mutating family) needs a decision on whether to replace the shared
   `SONDER_FILE_APPROVAL_CODE` with per-call approvals (the grant design).
4. **Whether `auto` should keep allowing `execution`-class tools unattended.**
   Today `auto` allows `run_code`/`workspace_run` with nobody present; A1 keeps
   that because the handoff says to preserve explicit `auto`. If you want
   `auto` to mean "edits and reads only" for unattended callers, that is a
   matrix change, not a decider change, and should be its own decision.
5. **Audit rotation.** `DurableToolAuditRepository` fails closed at 4,096 records
   or 8 MiB and has no rotation. Wiring it for reads will hit the bound in
   normal use. Options: rotate on bound (keep the chain head), or raise limits
   and add an operator command. Recommendation: rotation with the previous
   file's final digest carried into the new file's first record.
6. **Grant shape for slice 2.** Start from the existing `ToolScope` plus
   `approval_token`, adding `source`, `expires_at`, `nonce`, and `max_calls`;
   defer provider, token, and byte budgets until the model gateway has an
   authorization port at all. Confirm or amend.
7. **Where the A0 events live.** `operations.db` through the existing sink
   (recommended, durable and already redacted) versus a new bounded JSONL.

---

## 10. Perspective from other harnesses and from the model's side

These are my additions, not verified repository facts. They are here because
Nathan asked for them and because they change how the slices above should be
shaped.

**The nested-harness approval problem is the real design problem behind A1.**
When Claude Code or Codex calls Sonder over MCP, there *is* a person, one hop
up. The outer harness already has a permission prompt for every MCP tool call;
what it lacks is any way to know that Sonder's inner `ask` fired, because the
inner `ask` silently resolves to `allow`. Fail-closed is therefore not a
regression in capability if the refusal is *structured*: a stable error code
(`approval_required`), the decision fields, and an approval nonce bound to
(tool, argument digest, principal, expiry). The outer harness or its human
re-issues the call with the nonce; Sonder honors it once. That nonce is the
capability grant in miniature, and `ToolGatewayRequest.approval_token` plus
`ResourcePolicy.Decision.ALLOW_ONCE` are already the right slots for it. The
legacy MCP surface cannot carry a nonce without a schema change, which is one
more reason to make the native surface authoritative for one family first.

**Models treat refusals well and ambiguity badly.** A model given "policy
denied, not transient, choose another tool or finalize" changes course; the
repository's `HOST POLICY` text is already shaped this way and should be kept.
A model given a bare failure retries, often with a slightly different argument,
which is exactly how a fail-closed gate turns into a loop that burns the step
budget. So terminal reasons must reach the model *in the observation text*,
distinct from ordinary tool failure, before they are useful anywhere else. This
is why slice C introduces the vocabulary on tool receipts rather than on a
central enum nobody consumes yet.

**"Verifier unavailable" must not be readable as "verified".** Models will claim
completion whenever the transcript looks finished. The false-completion metric
(F in the handoff) needs a structured "final claim" event bound to the verifier
result that backed it; a claim made while the verifier was unavailable is
`unverified`, never `verified_complete`. The evaluation harness already has the
right discipline (graded versus infrastructure); the agent loop's final answer
does not yet emit anything a verifier can be bound to.

**Prefix stability matters as much as token count.** Local inference and
prompt caching both reward a stable prefix: system text, policy, tool schemas,
then the volatile sections. The section-budgeted planner's fixed section order
is the right shape; when it is shadowed, measure prefix churn between
consecutive turns, not only budget fit. A planner that fits the budget by
re-ordering sections every turn will cost more than it saves.

**Tool schemas for small local models.** Keep argument surfaces small, use
enumerations for flags, and never expose authorization parameters (`token`,
`approval`, `extra_roots`) to the model; a model cannot legitimately hold a
secret, and a schema field is an invitation to guess one. The native catalog
already strips these; the legacy catalog exposes them, which is a second reason
the mutating family is slice 2.

**Budgets should end runs, not truncate them.** When a step, token, or time
budget is exhausted, give the model one final turn with "budget exhausted:
summarize what was verified and what was not". The result is a partial-result
receipt with `terminal=budget_exhausted` and a usable summary instead of a
cut-off stream. The agent loop already forces host finalization at `max_steps`;
extend the same pattern to the other budgets as they gain a shared vocabulary.

**Fencing tokens belong on effects, not records.** Mint a monotonic token at
lease claim, carry it in every effectful call (tool receipt, compute job, batch
write journal), and have the receiver reject stale tokens. That is what turns
the autopilot's record-level fence into effect-level authority. The receipt
field is the prerequisite, which is why it is in slice C and the token is in
slice 2.

**Evaluation hygiene.** `pass@k` on a deterministic replay is degenerate; report
it only for live runs with fixed seeds where the backend supports them, and
always keep the raw per-trial outcomes. Keep held-out cases out of training
exports (the TRAIN-003 separation gate exists) and never let a model-graded
score gate promotion, which the case manifest already enforces. Pin the model by
content digest, not by alias; the history store already refuses anything else.

**Provenance of tool output.** Everything a tool returns is untrusted input to
the next model call. The prompt-injection provenance slice (SEC-006) exists;
the practical rule for the tool gateway is that no tool result may change
permission state, which the durable-authority set already guarantees for the
agent path. Keep that invariant in the parity tests for slice C.

---

## 11. Defects found along the way (fix regardless of the slice)

1. `permission_modes.decide(non_degrading=...)` is a dead parameter
   (`permission_modes.py:756`), documented at `:764-782` and `:400-406` as the live
   mechanism for `/selfmod deploy`. Nothing reads it and nothing passes it; the
   real check is `server.py:2573-2591`, which reaches into the private
   `_rule_action_for`. Remove the parameter and correct the docstring, or make
   the selfmod gate use it.
2. `ToolReceipt.policy_match` and `ToolReceipt.model` (`gateway_contract.py:149,152`)
   are never assigned; a consumer reading `""` as "no policy matched" would be
   wrong.
3. `typed_gateway.default_tool_context` hard-codes `source="repl"` and
   `auth_level="local"` for every typed tool call, so a native MCP call is
   recorded as a REPL call.
4. `ToolGateway.execute` emits no receipt on `Cancelled`, `DeadlineExceeded`, or
   `Forbidden` exits; the durable audit therefore cannot see refusals or
   timeouts.
5. `ToolApplicationFacade.compose` cannot accept an audit repository although
   `ToolGateway.from_typed_ports` can; the durable audit is unreachable from the
   composition root.
6. One HTTP chat turn is captured twice by `SessionCaptureService`
   (`serve.py:5555` and `server.py:5760`).
7. `serve.py:1288` lists the root `tool_contract` for live reload while
   `serve.py:58` imports the typed `authority_contract` under the same name;
   both can be live in one process.
8. `docs/architecture/migration-inventory.json` still names `workbench` and
   `file_ops` as root legacy modules; `WP8-NATIVE-MCP-MIGRATION.md` says 204/45
   where the catalogs now say 206/50; `REQUIREMENT-AUDIT-NEXT.md` says the ledger
   is all `planned`.

---

## 12. Defer or reject (agreeing with handoff section 11, with additions)

Agree with every deferral in the handoff. Additions:

- Do not build the twelve-field `CapabilityGrant` before a receipt can carry
  it; start from `ToolScope` plus an approval nonce (decision 6).
- Do not move `permission_modes` into the typed layer as a rewrite; adapt it
  behind `PermissionEvaluator`. A second precedence table is the failure the
  `Decision` docstring already names.
- Do not migrate the mutating file family in the same change as the reads; the
  approval-token model needs its own decision.
- Do not make the golden lane depend on a live model in CI; the cassette lane
  is the gate, `--live` stays an operator action.
- Do not introduce a central `TerminalReason` enum ahead of a consumer; grow it
  from tool receipts (C) to loop steps to jobs.
- Do not wire the context planner, lesson decay, or outbox in the first slice;
  each has a shadow step of its own in `docs/research/harness-landscape.md`
  (R7, R8, R9) that should run as written.

---

## Appendix A. Evidence anchors by subsystem

Authority: `permission_modes.py:40-49,214-243,263-267,406-409,433-460,744-756,764-782,844-886`;
`sonder_runtime/domain/execution/policy.py:16-40`; `permission_rules.py:386,410`;
`sonder_runtime/adapters/security/permission_policy.py:1-59`;
`sonder_runtime/interfaces/http/serve.py:2180-2215`; `reloadable_mcp.py:40-75`;
`sonder_runtime/bootstrap/native_mcp.py:570-660`; `server.py:2573-2591,2710,7570,7665,9745,13468,16782,16898`;
`sonder_runtime/interfaces/repl/repl.py:316-339,425,433`; `master_orchestrator.py:796`.

Typed tool seam: `sonder_runtime/application/tools/gateway_contract.py` (whole file);
`sonder_runtime/application/tools/facade.py:1-60,95-210`;
`sonder_runtime/application/tools/typed_gateway.py:53-66,84-125`;
`sonder_runtime/application/tools/resource_policy.py:28-35,66-160,259`;
`sonder_runtime/application/tools/audit.py`; `sonder_runtime/adapters/persistence/tool_audit.py:41-124`;
`sonder_runtime/application/execution/tool_service.py:45-89`;
`sonder_runtime/adapters/tool_executor.py:17-274`; `sonder_runtime/adapters/runtime_container.py:48-104`;
`sonder_runtime/bootstrap/app.py:1043`; `sonder_runtime/application/context.py:38-96`.

Runtime entry points: `sonder_runtime/__main__.py:593,654,656,660,669,673,677,696,698`;
`sonder_runtime/bootstrap/legacy_root.py:7-38`; `legacy_interfaces.py:9-20`; `legacy_mcp.py:22-38`;
`legacy_model.py:19-46`; `serve.py:134-152,2239,2253,2943,4834,5022,5065,5184,5423-5570,5816,5848`;
`repl.py:186,1654,2738`; `server.py:1384,1532,1591,1614,1852,1909,3399,3829,4156,4357,5468,5561,5790,5832,5912,25588`;
`adapters/model_bootstrap.py:23-43`; `adapters/inference/ollama_gateway.py:152,194`;
`adapters/inference/openai_compat_gateway.py`; `adapters/model_gateway_factory.py:46-68`.

Workbench and file family: `sonder_runtime/adapters/filesystem/file_ops.py:135,170,186,271,284,300,317,325,339,352,366,385,510,522,536,589,615,635`;
`sonder_runtime/adapters/filesystem/workbench.py:31-40,85`; `server.py:9250,9261,9542,9558,9853-12409`;
`docs/architecture/WP8-NATIVE-MCP-MIGRATION.md`; `docs/architecture/migration-inventory.json`.

Evaluation: `eval_harness.py:95,249,293,453,481,492,600,646,669,732,761,902,923-960,967,983,1125-1195`;
`sonder_runtime/application/evaluation/trajectory_replay.py:177,196`; `reproducible.py:49,58,69,642`;
`case_manifest.py`; `sonder_runtime/adapters/evaluation_history_store.py:63`; `verifiers.py:45,134,151`;
`solver.py:188,223`; `eval_models.py:14,33,77`; `eval_solver.py:53,56`; `eval_duel.py:52,70-75`;
`eval_retrieval.py:148,160`; `server.py:15140,15378`; `docs/evals/README.md`; `.github/workflows/ci.yml`.

Dormant and shadow components: `sonder_runtime/application/context_planner.py:15-26,77,79`;
`bootstrap/app.py:372,814-829,833,908-910,1081`; `adapters/context_planning.py:41`; `orchestrator.py:290,369,374,442`;
`lesson_decay.py`; `memory_quality.py:20,21,171,221,260,265,303`; `retriever.py:120-133,390,506,641`;
`scripts/nightly_self_improve.py:197`; `sonder_runtime/adapters/persistence/queued_actions.py:373`;
`adapters/persistence/sqlite/outbox.py:31,64,93`; `adapters/memory_repository.py:56`;
`application/memory/outcome_service.py:31,51-54,77`; `adapters/local_observability.py:204,224,268-270,434,596-600`;
`application/observability/trace_projection.py:47`.

Uncertainty, leases, budgets, populations: `command_recovery.py:167-177,382`; `sonder_launcher.py:77,786-800,1921,2138-2270`;
`adapters/persistence/served_action_receipts.py:5-7,42,116,169`; `serve.py:798-859`;
`adapters/persistence/fanout_store.py:51-57,510,745,806`; `server.py:23300-23312`;
`application/operations/startup_reconciliation.py:94,128`; `adapters/persistence/autopilot_store.py:44,76-79,399-443,509,738`;
`autopilot_controller.py:267-273,606-608,644-646`; `selfmod.py:70,88,114,1598-1685`;
`adapters/persistence/fleet_store.py:477-591`; `application/workflows/restart_recovery.py:19,34-41,143-150`;
`durable_locks.py:29-31`; `application/ports/subagents.py:45,199`; `domain/agents/roles.py:23`;
`model_gateway/health_and_roles.py:114`; `server.py:2330-2332,19481-19485,19700`; `isolated_runner.py:75,557,663`;
`domain/memory/rules.py:35-38,88-116`; `migrations/memory/0002_outcomes_source.py`; `calibration.py:55`;
`grounded_outcomes.py:58,141,185,209`; `domain/compute_fabric.py:316-318`; `application/compute_fabric/service.py:258-268`;
`application/compute_fabric/jobs.py:35,428-470`; `docs/runbooks/compute-fabric.md:203-209`.

## Appendix B. Test inventory by contract

Permission modes and gates: `tests/test_permission_modes.py` (101 tests),
`test_permission_gate_dispatch.py`, `test_permission_gate_http.py`,
`test_permission_gate_coverage.py`, `test_permission_durable_authority.py`,
`test_permission_rules.py`, `test_permission_context.py`,
`test_permission_policy_display.py`, `test_permission_policy_provider.py`,
`test_plan_mode_read_only_surface.py`, `test_risk_of_fail_closed.py`,
`test_selfmod_deploy_gate.py`, `test_native_mcp.py`, `test_app_permission_surface.py`.

Typed tool seam: `test_crosscutting_tool_gateway.py`, `test_seam002_typed_gateway.py`,
`test_execution_tools_facade.py`, `test_developer_sdk.py`, `test_tool_audit_repository.py`,
`test_remaining_tool_policy.py`, `test_remaining_tool_catalogs.py`, `test_tool_catalog_artifacts.py`,
`test_legacy_tool_executor.py`, `test_wp3_seam002_tool_contract.py`.

Cross-surface contracts: `test_tool_contract_conformance.py`, `test_command_catalog*.py`,
`test_agent_help_dispatch_drift.py`, `test_catalog_near_miss.py`, `test_tool_capabilities.py`,
`test_capability_visibility.py`, `test_native_tool_policy.py`, `test_mode_tool_policy.py`.

Interfaces: `test_wp8_openai_compatibility.py`, `test_api004_openai_integration.py`,
`test_wp1_http_model_facade.py`, `test_wp1_http_facade.py`, `test_wp1_http_runtime_injection.py`,
`test_serve_*.py`, `test_http_serve_*.py`, `test_mobile_parity_wire.py`, `test_wp8_mcp_compatibility.py`,
`test_mcp_stdio_transport.py`, `test_mcp_primitives.py`, `test_mcp_task_handler.py`,
`test_reloadable_mcp.py`, `test_api003_*.py`, `test_external_mcp.py`, `test_wp1_repl_facade.py`,
`test_wp1_repl_runtime_injection.py`, `test_repl_*.py`, `test_spec5_interfaces.py`.

Containment: `test_file_ops_paths_boundary.py`, `test_file_ops_containment_degradation.py`,
`test_file_ops_sensitive_read.py`, `test_file_ops.py`, `test_workbench_paths_boundary.py`,
`test_workbench.py`, `test_workbench_inline_shell.py`, `test_workbench_server.py`,
`test_harness_root_confinement.py`, `test_remaining_execution_containment.py`.

Boundary ratchets: `test_wp1_root_server_boundary.py`, `test_wp1_legacy_root_boundary.py`,
`test_lazy_legacy_model_boundary.py`, `test_wp1_main_root_removal.py`,
`test_wp1_model_adapter_root_removal.py`, `test_wp1_command_surface_root_removal.py`,
`test_wp1_master_orchestrator_boundary.py`, `test_server_source_invariants.py`,
`tests/production/test_architecture.py`, `tests/production/test_release_hardening.py`,
`tests/production/test_error_signal_ratchet.py`.

Evaluation and memory: `test_eval_harness.py`, `test_eval_harness_e2e.py`,
`test_reproducible_evaluation.py`, `test_evaluation_case_manifest.py`, `test_wp6_trajectory_replay.py`,
`test_eval_history.py`, `test_evaluation_history_service.py`, `test_promotion_eval.py`,
`test_wp6_promotion_gates.py`, `test_verifiers.py`, `test_outcome_source.py`, `test_calibration.py`,
`test_grounded_outcomes*.py`, `test_lesson_decay.py`, `test_memory_quality.py`,
`test_retrieval_policy.py`, `test_wp4_context_planner.py`, `test_context_planning_facade.py`.

Uncertainty, leases, compute: `test_autopilot_stale_lease.py`, `test_autopilot_store.py`,
`test_fleet_store.py`, `test_served_action_idempotency.py`, `test_launcher_idempotency_boundary.py`,
`test_wp9_reconciliation.py`, `test_startup_reconciliation_order.py`, `test_failure_injection.py`,
`test_compute_placement_service.py`, `test_compute_fabric_*.py`, `test_execution_world_port.py`,
`test_remaining_execution_world*.py`, `test_seam004_execution_world_integration.py`.

## 13. Status after implementation (2026-09-02, same day)

Nathan approved the design with one amendment: `auto` keeps executing tools
unattended (decision 4 resolved as "keep"; the `dangerous` class is still
refused unattended, matching the mode's blurb). Everything below landed on
`claude/sonder-runtime-commit-6d6e4v`; each commit's message carries the
detail.

| Part | State | Where |
|---|---|---|
| A0 receipts | done | `permission_modes` decision observers; `adapters/security/permission_receipts.py` routes every unattended refusal, and every unattended allow of anything but a safe read, to `operations.db` through the application event sink (`permission.unattended_refusal` / `permission.unattended_allow`) |
| A1 fail-closed | done | unattended `ask` is refused for `mutation`, `execution`, `dangerous` (`source="unattended"`, remedies named); `ask`-class proceeds on the record; `non_degrading` removed; copy, `/permissions` display and `policy_explain` updated; chain gates narrow a slash command to the member its argument reaches (`command_catalog.narrow_branch_tools`, bare forms included) so a read is never refused for a write it cannot perform; the console tells the control chain when an operator answered (`operator_approved`) |
| B evaluation lane | done | two suites gate CI (`smoke_python`, `tool_policy_gates`); statuses `verifier_unavailable`, `unknown`, `abandoned`; `tool_policy` scenario kind through the real gate with the scenario's own mode and rules; `compare --run A --run B`; `--trials k` with pass@1 / pass@k; history recorded by default only where the provider digest is honest; `harness_outcomes.py` owns the vocabulary and reuses `RegressionAssessment` |
| C typed read family | done | the seven reads run through `Application.tools` on both surfaces (`bootstrap/typed_tools.py`, `adapters/security/permission_evaluator.py`, `adapters/typed_tool_executor.py`); receipts carry `terminal`, `policy_match`, `evidence`, `source`/`auth_level`; early exits publish receipts; the durable audit rotates; the native line-range read gained the secret guard; see `evidence/TOOL-READ-FAMILY-TYPED-GATEWAY-2026-09-02.md` |
| Defects 1–7 | closed | 1 (`non_degrading`), 2–5 (receipt fields, context factory, early-exit receipts, `compose(audit=)`), 6 (the served turn is captured once: `answer_with_history(capture_session=False)` from `sonder_serve._run_prompt`), 7 (`sonder_serve` no longer live-reloads the root `tool_contract` under the typed contract's name) |
| Defect 8 | closed as far as honest | WP8 counts corrected (206 / 50); `migration-inventory.json` annotated as the historical 2026-08-21 snapshot it is (WP0-BASELINE already said so; no generator exists to regenerate it); `REQUIREMENT-AUDIT-NEXT.md` gained a dated addendum pointing at the new evidence, with the formal ledger untouched (204 `planned`, 0 `verified`) |
| Branch cleanup | integrated; deletion blocked | every remote branch classified in `RETIRED-BRANCHES-2026-09-02.md`; `claude/fable-skill-forge` and `docs/architecture-handoff` merged here; the remote refuses branch deletion from this session's credential, so the deletions are listed for the owner to run |
| Slice 2: mutating family | done | the nine mutating file tools run through `Application.tools` on both surfaces, legacy formats and structured refusals unchanged; `ToolScope.gate` makes the permission decision exactly once per call (the gateway for native, the surface's own gate for a legacy forward, recorded as `permission:surface`); the native canonical `write_file`/`make_directory`/`read_file` descriptors gained bounded schemas; see `evidence/TOOL-MUTATING-FAMILY-APPROVALS-FENCING-2026-09-02.md` |
| Slice 2: one-shot approvals | done | `call_digest`/`Decision.call_id`; `adapters/security/approval_ledger.py` (`approvals.db`); `decide()` spends an approval for exactly the refused call at step 5c or notes it pending and names `/approve <call id>`; every surface that has arguments passes them (legacy MCP, agent, control, served, native); `permission_approve` (dangerous, durable-authority, admin-bound, operator-only) and `permission_approvals`; matched by digest, so no nonce travels in any call (a deliberate departure from the §10 sketch) |
| Decision 3: the shared file approval code | retired | `SONDER_FILE_APPROVAL_CODE` no longer grants anything (warns once when set); a spent one-shot approval carries exactly the reach the operator approved (`file_ops.reach_scope`, installed by every deciding surface and the native surface, roots honoured with containment still checked); every agent path strips a model's string `token`/`approval` |
| Surface sweep | done | every command on every surface, the router and the CLI, in `manual` and `auto`; five defects found and fixed (fanout transport traceback, native inspection defaults, nine native tools misrouted, unbounded native run schemas, router permuted-name preference); the sweep redirects every module-local file root and fails a run that changes the checkout; `scripts/surface_sweep.py`; see `evidence/SURFACE-SWEEP-2026-09-02.md` |
| Slice 2: effect fences | done | `adapters/execution/effect_fence.py`; the autopilot worker (per task), every fleet worker thread (while bound to its agent row) and the selfmod editing agent fence their effects on their leases; `decide()` refuses effect classes on a lost fence before any policy (`source="fence"`, receipt); reads unfenced; the `tool_policy` evaluation lane pins fences, named refusals and decision sources |
| Redesign: terminal and app | done (2026-09-03) | one direction, "quiet instrument", drafted on a design canvas (`docs/design/sonder-redesign-2026-09-03/`) and implemented: the REPL's palette, packed header, gutter prompt and status line (`repl.py`, contracts kept), the app's token theme, bundled Plex faces, gutter transcript, composer, rail, settings and System sections (`app/lib/theme.dart` and screens); see `evidence/REDESIGN-QUIET-INSTRUMENT-2026-09-03.md` |
| Automatic tier escalation | done (2026-09-03) | the capability ladder and the gateway's escalation ceiling gained their live caller: `application/routing/tier_escalation.py` plans the distinct-model rungs (upward only, local only, at most 2 beyond the start, reasoning pre-route, `SONDER_MODEL_ESCALATION` knob) and the default route's chat turns (`_sonder_impl_serialized`, `_answer_with_history_impl`) and workbench agent runs (`_workbench_agent_escalating`) step up on a transport failure, an empty answer or an undrivable loop; explicit tiers and pins never move; `model_escalation` activity events; pinned by `tests/test_tier_escalation.py`; exercised live in `evidence/MODEL-TRIALS-2026-09-03.md`; the three recorded decisions were then taken narrowly (served `project` passes through only inside the configured roots, `workspace_root()` is the checkout again, the code gate's verified failure steps up) |

Not done, deliberately:

- `eval_solver.py`, `eval_duel.py` and `eval_retrieval.py` do not share the
  harness today, so they did not gain the history flag through it; wiring
  them through `eval_harness` is its own change.
- `SONDER_ISOLATED_APPROVAL_CODE` and `SONDER_ISOLATED_WRITE_APPROVAL_CODE`
  followed the file code on 2026-09-03: retired, warn once, grant nothing; a
  writable isolated workspace needs a one-shot approval of exactly that call;
  `ResourcePolicy.Decision.ALLOW_ONCE` is still unused; the compute job
  worker is not fenced (its claim is the job record and cancellation already
  reaches the process; the permission gate decides nothing there).
