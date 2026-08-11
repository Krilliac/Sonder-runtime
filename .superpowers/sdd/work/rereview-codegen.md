# Re-review — defect #35 fixes (`fd940c9`, report `51a9215`), parent `4e2a315`

Worktree `D:\sonder-wt\15-codegen-loop`, branch `work/15-codegen-loop`.
Everything below was produced by running the code. No `git stash`, no `git add -A`,
no live benchmark/campaign, no writes to the operator's store, no edits to
`app/build/**/local-system/*.py`. Mutations were applied in place and restored;
the tree was verified clean (`git status --short` empty) after every experiment.

**Test files run (only these):** `tests/test_grounded_outcomes.py`,
`tests/test_codegen_build_loop_server.py`, `tests/test_codegen_loop.py`,
and (once, to confirm the report's adjacency claim) `tests/test_eval_history.py`,
`tests/test_inspection_facade.py` → `27 passed in 1.67s`.

---

## A. Fix 3 (`elif` → second `if`) — what became reachable, and does fix 1 hold it?

**Newly reachable paths.** `_feed_grounded_outcome` (`server.py:7718`) is the *only*
call site of the whole module — `note_generation` and `attribute` are called from
nowhere else in the repo (measured: `grep -rn "grounded_outcomes\." --include=*.py`
outside the module and its tests returns exactly `server.py:7738/7741/7742/7743`).
The change therefore alters behaviour for exactly one input: a `name` in
`GENERATORS ∩ VERIFIERS`. That intersection is measured as `['codegen_build_loop']`
and is pinned by a test. So the newly-reachable path set is:

1. `codegen_build_loop` now runs `attribute()` after `note_generation()`, in the same
   call, on the same tool name.
2. Nothing else. A generator-only or verifier-only name takes the same branch as before.

**Dependency test — fix 1 reverted, fix 3 kept.** Driven through the real call site
with a build output carrying `[interaction_id: iid-SELF]`, `_record_outcome_signal`
captured:

| code state | only own generation pending | own + older `offload` generation pending |
|---|---|---|
| HEAD (fix1+fix3) | `writes=[]`, report `self_blocked=1` | `writes=[('iid-OFFLOAD','compiled')]` |
| **fix3 kept, fix1 REVERTED** | **`writes=[('iid-SELF','compiled')]`** | **`writes=[('iid-SELF','compiled')]`** |
| parent (no fix1, `elif`) | `writes=[]` | `writes=[]` |

**The dependency genuinely holds, and it is load-bearing.** Without fix 1, fix 3 makes
the tool write a `compiled` (+0.70) row against its *own* interaction id — the exact
self-graded population `4e2a315` was de-prioritising — and in the second column it also
*shadows* the legitimate older generation. Fix 1 is not covering merely the one tested
case: the guard keys on `pending.tool == kind`, and the only way a pending can be
created is `note_generation(match.group(1), name, ...)` at `:7741`, which always uses
the same `name` that `attribute()` is then called with at `:7743`. The guard's predicate
is therefore exactly the predicate of the newly-reachable path — total coverage, not
anecdotal. The one residual is name-identity: a generation noted under a *different*
name for the same work would not be caught. Checked and currently impossible —
`ensemble_answer` (`server.py:18506`), the model call inside the loop, never calls
`_record_direct_tool`, so it notes nothing.

**Can a generation be attributed twice, or by an unqualified verifier?** No.
Per-kind: `if kind in pending.judged: continue` still bars a second claim by the same
verifier kind, and the store enforces it again — `record_outcome_row`
(`sonder_runtime/adapters/memory_store.py:780`) is `INSERT OR IGNORE` behind the
`uq_outcomes_interaction_signal_nonnull` unique index built by
`_dedupe_outcomes_for_unique_index` (`:255`, applied `:381`). Different verifier kinds
judging the same generation is by design (`test_run` *and* `build_run`), unchanged.

**Gap (Important, new):** the test named for fix 3,
`test_a_dual_role_tool_is_routed_by_role_not_by_branch_order`, feeds an output with
**no** `[interaction_id: ...]` in it, so its GENERATORS branch is a no-op and the test
never exercises the dangerous combination at all. Confirmed by mutation: under **M1
(fix 1 removed)** that test still passes — nothing in the suite catches the call-site
self-grade proven in the table above. The safety argument is correct but unpinned.
Six-line fix: the same test with `"...[interaction_id: gen-self]"` appended to the
output and `assert written == [("gen-1", "compiled")]`.

## B. Fix 2 (`judged.discard` on a failed write)

* **A retry actually lands.** Measured end-to-end: `note_generation("i1","sonder")` →
  `attribute(test_run, record_fn=raises)` → `attribute(test_run, record_fn=ok)` gives
  `writes=[('i1','tests_passed')]`, `r1["recorded"] is False`, `r2["recorded"] is True`.
  The pending is not merely alive — the second attempt writes the row.
  Caveat worth stating: nothing retries automatically. `_feed_grounded_outcome`
  discards the report, so the retry is "the next time a `test_run`-kind verifier runs
  for that project inside `ATTRIBUTION_WINDOW_SECONDS = 900`" — a real second chance,
  not a guaranteed one.
* **No double count on a partial first write.** The only partial-success shape is
  insert+commit succeeding and the enclosing `conn.close()` in `_record_outcome_signal`
  (`server.py:7707`) raising. The retry then re-inserts — and is absorbed by the unique
  index above (`INSERT OR IGNORE`, `rowcount == 1` only on a genuine insert). Verified
  at the schema/migration level, not assumed from the docstring.
* **`recorded` vs `attributed` is honest.** Constructed case (one failed write, one
  successful retry): `{'noted': 1, 'attributed': 2, 'unlinked': 0, 'recorded': 1,
  'write_failed': 1}` against exactly one row written. `attributed=2` is correct as a
  count of *decisions* (two calls each reached the write), `recorded=1` is correct as a
  count of *rows*. Both individually right.
* **Minor, new:** `_STATS["unlinked"]` is still incremented on the self-blocked return
  (`grounded_outcomes.py:186`). Measured: self-blocked → `unlinked=1, self_blocked=1`;
  genuinely-empty → `unlinked=1, self_blocked=0`. The module's own docstring says the
  two facts "used to look identical"; they are now distinguishable only by subtracting
  `self_blocked`, and any consumer that reads `unlinked` alone still conflates them.
  This is the metric-blending shape, at reduced scale.
* **Minor, new:** `test_stats_count_rows_written_apart_from_attribution_decisions`
  asserts only `write_failed == 1` and `recorded == 1`. It never asserts
  `attributed == 2`, which is the split its name is about. It binds (M2 kills it), but
  it under-asserts its own claim.

## C. The self-grading guard — blocks, without over-blocking

`sorted(set(GENERATORS) & set(VERIFIERS)) == ['codegen_build_loop']` (measured; pinned
by `test_the_two_role_sets_overlap_on_exactly_one_tool`). Measured behaviour:

* self-attribution blocked: `attribute("codegen_build_loop", …)` over its own pending →
  `{'attributed': False, 'self_blocked': 1, 'reason': 'codegen_build_loop may not grade
  the work it generated itself'}`, `writes=[]`.
* **not over-blocked**: with the same pending, `attribute("build_run", …)` →
  `{'attributed': True, 'interaction_id': 'i9', 'signal': 'compiled',
  'generator': 'codegen_build_loop'}`, `writes=[('i9','compiled')]`. A different
  verifier still judges codegen work. (`test_run` likewise, via the existing test.)
* the `continue`-not-bail design is real and pinned. Extra mutation **M5**
  (`continue` → `break` inside the guard) →
  `1 failed, 86 passed` — `test_a_self_generated_row_does_not_hide_an_eligible_older_one`.
  A bail-out guard would silently discard every older eligible generation; the suite
  catches it.

## Mutation claims — all four re-run, all four confirmed

| Mutation | Claimed | Re-measured | Failing tests |
|---|---|---|---|
| M1 remove self-guard | 2 failed, 85 passed | **2 failed, 85 passed** | `test_a_tool_never_grades_the_work_it_generated_itself`, `test_a_self_generated_row_does_not_hide_an_eligible_older_one` |
| M2 remove `judged.discard` | 2 failed, 85 passed | **2 failed, 85 passed** | `test_a_failed_write_leaves_the_generation_still_judgeable`, `test_stats_count_rows_written_apart_from_attribution_decisions` |
| M3 restore `elif` | 1 failed, 86 passed | **1 failed, 86 passed** | `test_a_dual_role_tool_is_routed_by_role_not_by_branch_order` |
| M4 `SHRINK_FLOOR = 0.0` | 2 failed, 85 passed | **2 failed, 85 passed** | `test_a_shrinking_regeneration_never_replaces_the_file_on_disk`, `test_shrink_rejects_an_amputation` |
| M5 (added by this review) `continue` → `break` | — | 1 failed, 86 passed | `test_a_self_generated_row_does_not_hide_an_eligible_older_one` |

No count differs from the report.

### Does any other test pass for a neighbouring reason (the M4 error, repeated)?

Audited each of the five RED tests for a second sufficient cause:

* `test_a_tool_never_grades_the_work_it_generated_itself` — project `"p"` on both sides
  and `codegen_build_loop ∈ VERIFIERS` (pinned separately), so neither the
  project-mismatch path nor the not-a-verifier early return can be the cause. Binds on
  the guard. **Clean.**
* `test_a_self_generated_row_does_not_hide_an_eligible_older_one` — discriminates
  guard-present *and* guard-shape (M1 and M5 both kill it). **Clean, strongest of the five.**
* `test_a_failed_write_leaves_the_generation_still_judgeable` — the retry uses a
  *different, working* `record_fn`, so "the write silently no-opped" cannot produce the
  green. **Clean.**
* `test_stats_count_rows_written_apart_from_attribution_decisions` — binds, but
  under-asserts (see B). Not a neighbouring-reason pass; a weak assertion.
* `test_a_dual_role_tool_is_routed_by_role_not_by_branch_order` — **the one to flag.**
  Not a false pass, but it is *narrower than its name*: it proves the verifier branch
  runs, and proves nothing about the generator branch it now runs alongside, because its
  fixture output carries no interaction id. It survives M1 unchanged. Same family of
  error as the first shrink draft — the test exercises a neighbouring, safer path than
  the one the change made reachable. See the six-line fix in A.
* `test_a_shrinking_regeneration_never_replaces_the_file_on_disk` — carries in-test
  assertions `len(amputated) > 40` and `0.0 < ratio < 0.75`, which is exactly the
  discipline that the first draft lacked. **Clean.**

## The REFUTED claim — independent verdict: **REFUTED stands**

Re-derived by driving `server.codegen_build_loop` end-to-end with a stubbed
`workbench.run_program`, under a deliberately strict `error_regex = CS\d{4}` that
matches **none** of the synthesized notice lines (so a field can only change the verdict
if it is genuinely consumed, not by accidentally matching the regex):

| build dict | verdict line | green? |
|---|---|---|
| `ok=True`, clean output | `BUILD SUCCEEDED` | yes |
| `ok=False`, `timed_out=True`, empty output | `BUILD DID NOT RUN` | no |
| `ok=True`, `timed_out=True`, empty output | `BUILD DID NOT RUN` | no |
| `ok=False`, output `FAILURE: task failed` (no regex match) | `BUILD FAILED: 1 distinct error line(s)` | no |
| `ok=True`, `stdout_truncated=True` | `BUILD MEASUREMENT INCOMPLETE` | no |
| `ok=True`, `stderr_truncated=True` | `BUILD MEASUREMENT INCOMPLETE` | no |

All four fields (`ok`, `timed_out`, `stdout_truncated`, `stderr_truncated`) reach and
change the verdict, including the `timed_out`-only row where the exit status alone would
have said "green". The filed defect was **not** closed on a false negative; the fix at
`722ccd9` is real and still binding.

## Shrink floor — binds through the tool

`SHRINK_FLOOR = 0.75` (`codegen_loop.py:54`) → `shrink_rejected()` (`codegen_loop.py:437`)
→ consulted at `server.py:19053`. Two independent end-to-end observations:
the committed test (`attempt 2 rejected: shrank to …`, file byte-identical afterwards),
and — incidentally — five of the six REFUTED-claim probe runs above, which used a short
reply and printed `attempt 1 rejected: shrank to 5% of the original` on a code path this
review wrote from scratch. M4 kills the end-to-end test, so it binds on the floor itself,
not on the `<40-byte` near-empty rule.

## Accounting and the 17.70s → 1.63s drop

Reproduced independently, same three files:

| run | source | tests | result |
|---|---|---|---|
| baseline | `4e2a315` | `4e2a315` | **79 passed in 1.88s** |
| RED | `4e2a315` | `HEAD` | **5 failed, 82 passed in 1.87s** — the exact five named in the report |
| GREEN | `HEAD` | `HEAD` | **87 passed in 1.58s** |

`82 + 5 = 87 = 79 + 8` reconciles, and the RED run was captured at the final item count
(87 collected, none skipped, no collection error — every failure an `AssertionError`).

**The timing drop is not a floor.** The 17.70s does not reproduce: the same baseline
runs here in 1.88s, and a deliberately cold run (all 17 `__pycache__` trees removed,
`-p no:cacheprovider`) takes 2.31s. Item counts, not the clock, are the invariant that
proves both runs reached the same stage, and they do — 79 collected and executed at the
parent, 87 at HEAD, delta exactly the 8 added tests, with the 5 RED failures accounted
for individually by name and message. The 17.70s was environment (cold OS file cache on
D: for a fresh worktree, plus sibling-worktree agents competing for a 16 GB box), not
work skipped in the faster run. Nothing was truncated, capped, or aborted early.

## Anchors — all re-resolved programmatically

`grounded_outcomes.py`: `VERIFIERS` table **62**, `codegen_build_loop` verifier entry
**70**, `GENERATORS` table **77**, generator entry **79**, `_candidate` **146**, self
guard **162**, `attribute` **173**, `recorded` increment **216**, `judged.discard`
**223**. `codegen_loop.py`: `SHRINK_FLOOR` **54**, `score` **325**, `shrink_rejected`
**437**, `format_report` **446**. `server.py`: `_record_outcome_signal` **7707**,
`_feed_grounded_outcome` **7718**, GENERATORS `if` **7738**, VERIFIERS `if` (fix 3)
**7742**, `ground_artifact` **4480**, `artifact_verify` **10806**, `artifact_ground`
**12334**, `ensemble_answer` **18506**, `_codegen_build` **18849**, `codegen_build_loop`
**18898**, shrink consult **19053**. Every anchor in the report resolves. Two stale
numbers in the report's *inventory snippet* only (`build_run def 9986` → actually
**9994**, `test_run def 9623` → actually **9631**); no anchor in the prose is wrong.
(The review brief's "sole call site `server.py:7730`" is stale; it is **7742**.)

## New findings

* **Important — no test pins the safety dependency fix 3 rests on.** Detailed in A.
  Removing fix 1 leaves the suite green on the call-site self-grade that fix 1 exists to
  prevent. Six-line fix, same file.
* **Important — the report's inertness finding understates by half.** It flags 3 of 11
  `VERIFIERS` as never reaching `_record_direct_tool`. Measured over both tables:
  **10 of 19 `GENERATORS` are inert too** — `sonder`, `offload`, `agent`,
  `workbench_agent`, `consult`, `ensemble_answer`, `improve_function`, `apply_learned`,
  `scaffold_project`, `codegen_build_loop`. That list is every learning-tier tool, i.e.
  every tool whose reply actually carries the `[interaction_id: …]` tag that
  `_INTERACTION_ID_RE` (`server.py:7704`) matches on; the generators that *do* reach the
  recorder are the file/patch tools, whose output contains no such tag. Since
  `_feed_grounded_outcome` has exactly one call site and the only dynamic-name call
  (`server.py:843`) is an `ok=False` error path, the practical consequence is that
  `note_generation` almost never fires in production, so `attribute` has almost nothing
  to judge. The report's sentence *"Every other name in both tables either reaches the
  recorder or is a verifier that does"* is measurably false. This is a pre-existing
  wiring gap, not something this change introduced — but it means any coverage figure
  read off these tables overstates reality far more than the report says, and it lowers
  the live severity of defect #35 itself while raising the value of the wiring work the
  report deliberately deferred.
* **Minor — `unlinked` blends self-blocks with "nothing was waiting".** Detailed in B.
* **Minor — `test_stats_count_rows_written_apart_from_attribution_decisions`
  under-asserts its own name** (never checks `attributed == 2`). Detailed in B.

Nothing found that the change itself breaks. The three fixes do what the report says,
the dependency between them is real and now proven by execution rather than by argument,
and all four mutation counts reproduce exactly.

## Verdict

**MERGE.** Behaviourally correct and well-guarded; every claim I could test held, and the
one place the reasoning was unverified (fix 1 → fix 3) is now verified by direct
execution and holds. Add the six-line call-site regression test from A on this branch
before merging — it pins the review's central risk and needs no new round.

---

Re-reviewed 2026-08-11 on `work/15-codegen-loop` @ `51a9215`. Tree left clean and on
branch; mutations applied in place and restored (verified by `git status --short`).
