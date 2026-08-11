# Final residuals: #61

Branch `work/30-final-residuals`, base `fb597e4`. Two commits, clean checkout,
nothing pushed. No `git stash`, no `git add -A`, no sibling worktree touched.
`scripts/select_regression_tests.py` is **absent on this lineage** (checked
`scripts/`), so regression files are named deliberately below. Planted repro
files were held in the session scratchpad, outside the repo.

## Lineage — verified in this worktree

`git merge-base --is-ancestor <x> HEAD`:

| ref | ancestor of HEAD? |
|---|---|
| `feat/verified-fetch-modes-calibration` (`9f377f1`) | **yes** |
| `fb597e4` (stated base) | **yes** — HEAD exactly, before my commits |

## ITEM 1 — the inversion does reach `activity_tracker`, and further

### Reproduced, by execution, before any edit

`server._agent_dispatch_observed` (`server.py:16003` pre-edit) computes
`ok = not str(observation).startswith("ERROR:")` and hands the **same flag** to
two consumers: `activity_tracker.record_tool_result` (line 16006) and
`_feed_grounded_outcome` (line 16038). Only the second was fixed in `604e662`.

Real path, a project whose only content is one genuinely failing pytest:

```
harness_tools.test_run(root=proj)  -> {'ok': False, 'returncode': 1, 'framework': 'pytest'}
rendered observation, first line   -> 'test run (pytest)'   ('  ok: False' on line 4)
str(observation).startswith("ERROR:") -> False
activity_tracker tool_call event   -> ok=True                <-- inverted
activity_tracker._public_event(..) -> phase 'completed', ok True
format_transcript                  -> the success marker, not '×'
```

Three surfaces, not one. `agent()` embeds `format_end_report` +
`format_response` (which calls `format_transcript`) in the string it returns, so
the inverted marker is read by both the model and the operator.

The direct MCP path is **not** affected: `server.test_run` passes
`ok=data.get("ok", False)` — the verifier's real dict. So the same failing suite
was marked `×` when run directly and `•` when run through the agent loop.

### Wider than filed — measured, twice over

**(a) 19 non-verifier tools were never covered at all.** AST over `server.py`
intersected with `tool_capabilities.dispatch_names(server._agent_dispatch)`:
**26** dispatchable tools render their own verdict, and **19 are not in
`VERIFIERS`** — so the `VERIFIERS`-scoped outcome fix never reached them, and
`activity_tracker` was their only consumer. Measured: `script_run` on a script
exiting 3 renders `  ok: False`, activity recorded `ok=True`.

**(b) NEW CRITICAL — `604e662` closed the inversion for only 2 of 6 renderers.**
There are six verdict renderers in this codebase, not the two
`rendered_verdict`'s docstring named. One failing result rendered through each:

| renderer | verdict line | `rendered_verdict` (before) |
|---|---|---|
| `server._format_run_result` | `  ok: False` | `False` |
| `code_runner.format_result` | `status: failed` | `False` |
| `code_runner.format_project_result` | `project status: failed` | **`None`** |
| `artifact_grounding.format_result` | `artifact grounding: FAIL` | **`None`** |
| `server.artifact_verify` (inline) | `artifact verification: FAIL` | **`None`** |
| `isolated_runner.format_result` | `isolated status: failed` | **`None`** |

`None` means "no opinion", so `_feed_grounded_outcome` kept the caller's
inverted `ok=True`. The four unread shapes belong to **VERIFIERS** —
`run_project` (`compiled`), `ground_artifact` / `artifact_ground`
(`accepted`), `artifact_verify` (`accepted`). On the agent path a **failing**
verification by any of them was still being written to the **outcome store** as
a pass at **+1.0**, after the commit that was supposed to stop exactly that.
`isolated_run` shares the defect shape but is not agent-dispatchable and its
direct path passes a real dict, so it was not a live path; it is covered anyway.

### Fixed

* `grounded_outcomes._RENDERED_VERDICT_FIELDS` — all six shapes, as a table
  derived by reading the renderers. `rendered_verdict` reads the table.
* `server._RENDERED_VERDICT_TOOLS` (25 names) + a read in
  `_agent_dispatch_observed`. It can only turn a claimed success into a
  failure, never the reverse, so a dispatcher fault stays a failure.

**Scoped, not blanket — and that is measured, not argued.** `file_read` of a
23-byte YAML whose first line is `ok: false` makes `rendered_verdict` return
`False`. Reading every observation would let *file content* file a successful
read as a failure: the same defect pointing the other way, driven by input the
caller supplies. Pinned by `test_tool_content_cannot_flip_the_verdict`.
`archive_create`/`archive_extract`/`artifact_risk_inspect` also call a
`format_result`, but theirs is a `json.dumps` whose `"ok": false` keeps its
quotes and is not a verdict line; they are deliberately absent from the set.

### How many rows are affected — the honest answer

**For `activity_tracker`: none, and no record survives to count.** It has no
persistence — zero `sqlite3`/`connect`/`.db` references; state is
`_ACTIVE` (≤20 spans), `_LATEST`, and `_EVENT_RING` (512 events), all in
memory and discarded at process exit. `local_observability` states in its own
header that there is "deliberately no exporter, network path, background
thread, persistence". The memory DB has no tool-call table (26 tables listed,
read-only). So the display was wrong for every agent-path verification ever run,
and **we cannot tell retrospectively how many** — nothing was written down.

**For the outcome store, the residual in (b) is unquantifiable for the same
reason the `outcomes` table already has.** Read-only (`mode=ro&immutable=1`),
`C:\Users\natew\AppData\Local\sonder\memory.db`: **9,450** rows, columns
`(interaction_id, signal, reward, ts)` — **no source column**, so a machine
verdict is indistinguishable from a human one, forever, and no query can
separate rows written through the inverted path from correct ones. Additionally,
the pre-existing `note_generation` inertness finding means the auto-attribution
path may never have written anything yet; that is a reason the *stored* harm may
be nil, not evidence that it is.

## ITEM 2 — RED confirmed on this branch; ruling: an over-narrow double

```
tests/test_agent_tools.py::test_agent_runs_tool_then_final
TypeError: <lambda>() got an unexpected keyword argument 'repository_extra_roots'
```

**Ruling: (a) an over-narrow double.** Read before changing. Evidence:

1. **The double mirrors the wrong function.** It patches `_agent_dispatch` but
   carries `_agent_dispatch_observed`'s parameter list: it declares
   `project=""`, which `_agent_dispatch` has **never** had, and omits
   `repository_extra_roots`, which it has had since `7a4d0e9`. Measured
   signature: `(tool_name, args, allow_web=True, read_only=False,
   allow_location=False, repository_extra_roots='')`. A double that never
   matched the function it replaces cannot be pinning its API.
2. **No assertion concerns the dispatcher's parameters.** The four assertions
   are the final text, the activity report, the `tool calls:` line, and the
   observation reaching `prompts[1]`.
3. **The rejected argument is load-bearing.** `repository_extra_roots=project`
   is the only channel granting the host-selected root; passing it on the write
   arm too is what stopped 23 developer-workflow tools raising
   `PermissionError` on the very project the host bound.
4. **That requirement is already pinned properly elsewhere** — end-to-end,
   against the real `_resolve_root` rather than a double, by
   `test_harness_root_confinement.py::test_a_write_enabled_run_reaches_the_tool_on_its_bound_project`.
   So making the double agnostic loses no coverage.
5. **Convention:** 61 signature-agnostic doubles in this same file. The two
   other explicit-kwarg doubles (lines 1159, 1189) target
   `_agent_dispatch_observed`, whose signature they match correctly. This one
   is the outlier.

**Not this branch's regression.** At `06c2f79` the call site passes
`repository_extra_roots=project` verbatim and the double lacks it there too, so
the test was already RED before `5ec09dc`/`f1adfac`/`604e662`.

Fixed by making the double `lambda *a, **k:` with the reasoning recorded in
place. Proved it still binds by mutation (below).

*Note, not changed:* the two doubles at lines 1159/1189 are the same latent
trap (explicit kwarg lists for a function they double). They are GREEN today
because they happen to match; left alone rather than churn passing tests.

## ITEM 3 — `_record_outcome_signal`: reported, deliberately NOT fixed

It calls `memory_store.record_outcome_row` directly. Validation is **not** a
gap — both paths reject an unknown signal and a non-canonical reward. Compared
against `record_outcome_and_claim_lesson_distillation`, it skips exactly three
things:

1. **The interaction-existence precondition.** The wrapper returns early when
   `interactions` holds no such id; `record_outcome_row` inserts regardless.
   Measured: 0 orphan rows in `outcomes` today, so this has cost nothing yet.
2. **The `lesson_usage` credit** — `UPDATE lesson_usage SET outcome_signal,
   reward, outcome_ts WHERE interaction_id=?`. Condition: only when the outcome
   row is **newly inserted**, and only for rows carrying that interaction_id
   (i.e. only when a lesson was actually retrieved and used).
3. **Lesson distillation** — `_claim_distillation` on good evidence, or
   `_cancel_live_distillation` on absent/contradictory evidence, with
   process-liveness ownership.

**Why not fixed.** Verified the SQL myself: `lesson_usage_stats` aggregates
`AVG(CASE WHEN reward IS NOT NULL THEN reward END)` over `lesson_usage` with no
source filter, and `retriever.lesson_quarantine` reads it. So routing the bypass
through the wrapper would make **machine-attributed verdicts drive live lesson
eviction** — while `outcomes` has no source column to tell them from human ones.
That does not fix a blended metric, it creates one, which is the defect class
this fleet is closing. It is a design decision with a live blast radius, not a
small fix. Left, with the asymmetry recorded.

Read-only measurement, stated with its limit: **26** `lesson_usage` rows have
`reward IS NULL` while an outcome exists for their interaction — the shape this
bypass produces. That is an **upper bound on the observable footprint, not a
measurement of the bypass**: a duplicate signal through the wrapper (which skips
its update when `outcome_inserted` is False) and a `lesson_usage` row inserted
after its outcome leave the identical shape, and nothing distinguishes them.

## Mutation results — every guard planted, observed failing, reverted

| guard | mutation | result |
|---|---|---|
| activity verdict read (`server.py`) | `if False and ...` | **7 failed, 9 passed** |
| the four added renderer shapes | table cut back to the original two | **2 failed, 14 passed** |
| both fix hunks together (pre-fix state) | both of the above | **9 failed, 7 passed** |
| the repaired double still binds | `_agent_dispatch_observed` returns `""` | **1 failed** (`test_agent_runs_tool_then_final`) |

Each reverted and re-verified GREEN; `git status` clean afterwards, no residue.

## Verbatim pytest lines

RED, at the FINAL item count (16), both fix hunks reverted:

```
9 failed, 7 passed in 5.85s    tests/test_activity_verdict.py
1 failed, 486 passed in 20.87s tests/test_agent_tools.py::test_agent_runs_tool_then_final (item 2, in the regression set)
```

GREEN, final:

```
16 passed in 5.52s   tests/test_activity_verdict.py
136 passed in 7.34s  tests/test_agent_tools.py tests/test_harness_root_confinement.py
647 passed in 24.26s (17 named regression files)
```

Regression files named deliberately (no selector on this lineage):
`test_activity_verdict test_grounded_outcomes test_grounded_outcomes_infrastructure
test_grounded_outcomes_agent_dispatch test_agent_dispatch_dev_tools test_agent_tools
test_agent_verification_gate test_verification_examines_work test_activity_redaction
test_harness_root_confinement test_autopilot_controller test_autopilot_server
test_learning_health test_workbench test_reasoning_exposure test_memory_store
test_read_only_agent_policy`

The full suite (~522s) was **not** run.

## Commits

```
84cd79b  The other consumer of the same inverted flag (#61)
9836d8a  The double mirrored the wrong function's signature (#61)
```

## NEW findings

**Critical — `604e662` closed the inversion for 2 of 6 renderers; four
VERIFIERS were still filing failures as +1.0 passes.** Detailed above with the
measured table. Fixed here. The lesson generalises: `rendered_verdict` was
written from the two renderers its author had in front of them, and nothing
re-derived the set — which is why the fix here is a table plus a test that
renders a failing result through **every** renderer and reads it back.

**Important — the inversion's blast radius was 26 tools, not the 11 VERIFIERS.**
19 dispatchable tools render a verdict and are not verifiers (`git_merge`,
`git_commit`, `apply_patch`, `workspace_run`, `script_run`, the whole
`dependency_*` family). A scope drawn around "verification" missed most of the
tools whose failures were being displayed as successes.

**Important — `rendered_verdict` is unsafe to apply to arbitrary tool output.**
Measured: `file_read` of a 23-byte YAML beginning `ok: false` returns `False`.
Any future caller tempted to apply it without a tool scope will invert
successful reads, with the trigger being file content the caller supplies.

**Important — `outcomes` has no source column, and this now costs a second
answer.** It was already impossible to tell a machine verdict from a human one;
it is now also impossible to say how many rows the four unread renderers
mis-filed, or how many `lesson_usage` gaps the `_record_outcome_signal` bypass
caused. Two separate lanes have now been unable to quantify their own findings
for this one missing column. A `source` column is the cheapest thing on this
backlog and the one that keeps compounding.
