# Issue #510 guard inventory and canaries (2026-09-23)

Scope: Issue #510 section 6 ("Critical-rule promotion: prompt -> telemetry ->
runtime guard") and section 7 ("Guard canaries"). Requirement mapping is to the
[master implementation specification](../SONDER-MASTER-IMPLEMENTATION-SPEC.md).

A guard counts as **canaried** here only when a test deliberately drives the
production code path until the guard fires and asserts the guarded work did
not happen. A code path that exists but that no test trips is listed as
"no canary" even if the code looks right.

## Method

- Static search of `server.py`, root runtime modules, and `sonder_runtime/`
  for each candidate guard, under several naming conventions.
- For every guard this lane added or canaried, a RED proof: the canary was
  run with the guard disabled (wiring stashed, or the guard condition forced
  false in a scratch copy) and failed, then run with the guard restored and
  passed. Results are recorded under [Verification](#verification).
- Functions are named rather than line numbers, because `server.py` moves
  under concurrent lanes.

## Inventory

| # | Candidate guard | Status before this lane | Enforcement (function) | Canary test(s) |
|---|---|---|---|---|
| 1 | Identical retry fingerprint / repeated failed call | Present, canaried | `server._agent_turn`: `failed_call_counts` keyed by `_agent_call_signature`; third identical failed dispatch refused, fourth ends the run | `tests/test_agent_tools.py::test_agent_stops_repeating_identical_failed_tool_call`, `::test_agent_retry_guard_blocks_same_call_when_failure_text_changes` (PR #528) |
| 2a | No-progress loop across distinct failed calls (agent loop) | Present, canaried | `server._agent_turn`: `semantic_no_progress` window of 6; 4 same-class failures across 3+ signatures ends the run | `tests/test_agent_tools.py::test_agent_stops_semantic_no_progress_across_distinct_failed_calls` |
| 2b | No-progress verification loop (generate -> build -> repair) | **Missing: added by this lane** | `server.codegen_build_loop` + `sonder_runtime/domain/verification_progress.py` | `tests/test_verification_no_progress_guard.py` (new) |
| 2c | Autopilot failure budget | Present, no canary | `autopilot_controller` failure budget marks the run `blocked` | none found (cycle and replan budgets are canaried in `tests/test_autopilot_controller.py`) |
| 3 | Repeated identical tool call | Present; web branch had no canary | `server._agent_turn`: cached inspection results (3rd repeat forces final or ends run); identical successful web call refused | inspection: `tests/test_agent_tools.py::test_mutating_agent_still_stops_repeated_identical_successful_inspection` and siblings; web: `tests/test_issue510_existing_guard_canaries.py::test_canary_identical_successful_web_call_is_not_dispatched_twice` (new) |
| 4a | Output-token velocity (cloud) | Present, canaried | `sonder_runtime/adapters/bounded_cloud_generation.py` `bounded_cloud_generate`: per-call and per-turn output budget shared with the claim reviewer | `tests/test_server_helpers.py::test_bounded_cloud_agent_generate_caps_aggregate_output` and siblings |
| 4b | Tool-call and runtime budget (selfmod candidate) | Present, no canary | `server._selfmod_agent_policy` counters | `tests/test_issue510_existing_guard_canaries.py::test_canary_selfmod_tool_call_budget_refuses_the_next_call`, `::test_canary_selfmod_runtime_budget_refuses_after_deadline` (new) |
| 4c | Request-rate velocity (requests per unit time) | **Missing** | `sonder_runtime/domain/token_bucket.py` has no production caller; the OpenAI-compatible gateway only reports a provider 429 | none |
| 5 | Context growth | Present, canaried | `sonder_runtime/domain/agents/observation_prompt.py` bounds the model-visible observation window; `_post_model` compacts once on overflow | `tests/test_agent_tools.py::test_agent_observation_prompt_bounds_context_and_keeps_recent_evidence`; `tests/test_context_overflow.py::test_persistent_overflow_stops_after_one_compaction` |
| 6 | Duplicate worker | Present by key, canaried; scope-level duplicate not detected | `SQLiteWorkerRegistry.admit` (`DuplicateWorkerError`), fanout/fleet leases | `tests/test_worker_registry.py::test_active_duplicate_resume_key_is_rejected_atomically`, `tests/test_orchestration_durability.py::test_retry_lease_is_exclusive_until_released` |
| 7 | Fanout | Present, partly canaried | subagent depth/child/concurrency admission; master-orchestrator worker caps; lane caps; fanout `MAX_MODELS`; `cloud_workers` clamp | `tests/test_agent007_budget_admission.py`, `tests/test_master_orchestrator.py::test_per_run_worker_cap_respects_operator_and_compiled_ceilings`; no canary for `MAX_MODELS`, lane capacity, or the `cloud_workers` clamp |
| 8 | Dangerous concurrent Git/worktree operation | **Missing: added by this lane** | `sonder_runtime/adapters/git_mutation_guard.py`, wired into all seven `harness_tools` Git mutations and `git_tools.runtime_update` / `runtime_stash` | `tests/test_git_mutation_guard.py` (new) |
| 9 | Orphan-process reaper | Present, canaried | job registry `reconcile_with_cleanup` via `ProcessTreeSupervisor`; `ollama_lifecycle.cleanup_orphaned_discovery_probes` | `tests/test_remaining_agent_005_job_integration.py::test_recovery_executes_only_bounded_cleanup_and_requires_complete_receipt`, `tests/test_ollama_lifecycle.py::test_cleanup_terminates_only_stable_trusted_orphan_discovery` |
| 10 | Expensive/top-tier spawn cap | Partial: output-token budget and cloud opt-in only | no per-run count of cloud/top-tier spawns in router, orchestrator, or fanout | token budget canaries only (4a) |
| 11 | Batching/coalescing | **Missing** (only repeat detection) | repeat guards (1, 3) exist; nothing detects N distinct single `file_read`/`file_edit` calls or steers to `file_batch_write` | none |

## Guards added by this lane

### Concurrent Git mutation guard (candidate 8)

`guard_git_mutation(root, operation)` holds one in-process slot per working
tree (nearest ancestor with a `.git` entry; a linked worktree is its own key).
A second host-driven mutation on the same tree waits at most 2 s, then is
refused with `ConcurrentGitMutation` (a `PermissionError`) naming the holder.
An `index.lock` held by another Git process is reported as its own refusal
and is never deleted. That holder may be a brief concurrent read such as
`repo_status` or a crashed process, so the refusal says so instead of assuming
a crash. Linked worktrees are checked against their private git directory
(absolute or relative `gitdir:`).

Recovery is bounded and materially different from the blocked action: the
mutation is not queued or retried; the result tells the caller to wait for the
holder, re-inspect with `repo_status`, and decide again. `harness_tools`
mutations return `{"ok": False, "guard": "git_mutation_concurrency", ...}`,
and the MCP git tools render the `guard`, `holder`, and `recovery` fields
through `format_run_result`; the runtime-source MCP tools report `refused: HOST GUARD ...` through their
existing `PermissionError` handlers. The status probe and the fast-forward in
`runtime_update` now run inside one slot, closing its check-then-act window
against tool mutations in the same process.

### Verification no-progress guard (candidate 2b)

`codegen_build_loop` previously accepted any `attempts` value and re-sent the
same prompt to the model ensemble on every attempt regardless of outcome.
Now `bounded_attempts` clamps attempts to 1..6, and the report states when
a requested count was clamped. `VerificationProgressGuard` stops regenerating
a file when two consecutive observed attempts return an identical failing
outcome and the score did not improve. The outcome is fingerprinted as a
sorted list of whitespace-normalized error lines with duplicates kept, so
fewer copies of the same error count as a change. The best version so far is
kept and the report names the stall and its fingerprint, directing a change
of spec or approach instead of another identical attempt.

What restarts the streak: a clean outcome, a different fingerprint, or an
improved score. What the guard never sees: an attempt whose errors cannot be
compared. That means the host placeholder for a failing build whose output
matched no `error_regex` line, the truncated-output marker, or any error that
`codegen_loop.count_unreliable` classifies as masking (parse, declaration,
compiler error-limit, or partial output). Such an attempt resets the streak
instead of counting toward a stall, because equal floors say nothing about
whether the real errors changed; those loops run to the attempt cap. Note
that `codegen_loop.count_errors` already de-duplicates identical lines within
one build, so the duplicate-keeping fingerprint matters for other callers of
the guard rather than for this loop. The default two-attempt contract is
unchanged.

## Verification

Commands run locally on Windows 11, Python 3.12.10, in a fresh venv from
`requirements-dev.txt`:

| Check | Result |
|---|---|
| `python -m pytest tests/test_git_mutation_guard.py -q` | 8 passed |
| same, with `harness_tools.py` and `git_tools.py` wiring stashed (RED) | 5 failed, 3 passed (all 4 canaries failed) |
| `python -m pytest tests/test_verification_no_progress_guard.py -q` | 6 passed |
| same, with the `server.py` wiring stashed (RED) | 2 failed (both canaries), 4 passed |
| `python -m pytest tests/test_issue510_existing_guard_canaries.py -q` | 5 passed |
| same, with the web-repeat and both selfmod budget conditions forced false (RED) | 3 failed (all canaries), 2 passed |

Review follow-up (PR #553). New tests were added first and run against the
previous head, then against the fix:

| Check | Before fix | After fix |
|---|---|---|
| `tests/test_verification_no_progress_guard.py` | 7 failed, 7 passed | 14 passed |
| `tests/test_git_mutation_guard.py` | 2 failed, 10 passed | 12 passed |

The two linked-worktree `index.lock` tests passed before the fix (the check
already existed); they add coverage, not a RED proof.

Regression suites and repository checks are listed in the pull request.

## Requirement mapping

| Requirement | Ledger revision | Why |
|---|---|---|
| AGENT-008 (isolated workspaces; reconcile concurrent Git changes without force-overwriting another session) | 3, `implemented_unverified` | concurrent Git mutation guard |
| LOOP-007 (bounded retries) | 3, then 4, `implemented_unverified` | verification no-progress guard; web-repeat canary; revision 4 narrows the stall rule (duplicates kept, score must not improve, non-comparable outcomes excluded) |
| AGENT-007 (budgets) | 3, then 4, `implemented_unverified` | codegen attempt clamp; selfmod tool-call and runtime budget canaries; revision 4 adds reporting of a clamped attempt count |

All three stay `implemented_unverified`: the canaries are focused tests with
fake Git and fake compiler/model boundaries, not an end-to-end multi-lane run.

## Limitations and remaining gaps

- The Git slot is per process. Two Sonder processes mutating one tree are
  still protected only by Git's own `index.lock`. The shared `refs/stash` of
  linked worktrees is not serialized across worktrees. Git calls made outside
  `harness_tools` and `git_tools` (for example selfmod's own worktree
  management) are not wired to the guard.
- The no-progress guard covers `codegen_build_loop` only. The autopilot
  failure budget still has no canary.
- Still missing: request-rate velocity limiting on model calls (4c), a
  per-run cap on cloud/top-tier spawns (10), scope-level duplicate-worker
  detection (6), and batching/coalescing (11).
- Still without canaries: fanout `MAX_MODELS`, interactive-lane capacity, and
  the fanout `cloud_workers` clamp.
- Guard state is in-memory telemetry (`guard_snapshot`); it is not persisted
  across restart (Issue #510's durable guard-state item remains open).
