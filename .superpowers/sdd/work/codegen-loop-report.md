# Defect #35 — `codegen_build_loop` grades its own homework / discards its result dict

Worktree `D:\sonder-wt\15-codegen-loop`, branch `work/15-codegen-loop`, parent `4e2a315`
(`fix/sweep-backlog`, itself on `feat/verified-fetch-modes-calibration @ 9f377f1` — **not** `main`).

All line anchors below were re-resolved programmatically against the committed tree,
not copied from the filing.

---

## 1. Map of the loop

`codegen_build_loop` is `server.py:18898`–`19088`. It contains no other tool call
than `ensemble_answer`; the compiler is reached through `_codegen_build`
(`server.py:18849`) → `workbench.run_program`.

| Stage | Where | What happens |
|---|---|---|
| **Generate** | `server.py:19049` `reply = ensemble_answer(prompt, tiers=..., mode="code")`, cleaned at `:19050` `codegen_loop.strip_code`, mechanical slips at `:19051` | one file at a time, from a per-file spec plus the API *extracted from* the siblings already written |
| **Verify** | `run_build()` (`server.py:18966`), called at `:19024` (baseline), `:19063` (per attempt), `:19087` (final). It calls `_codegen_build` → `workbench.run_program`, then `codegen_loop.count_errors` | the verifier is the **project's real compiler**, not the model. `_codegen_build` returns `(combined output, exited-cleanly)` and consumes `ok` / `timed_out` / `stdout_truncated` / `stderr_truncated` from the returned dict (`server.py:18878`–`18894`) |
| **Keep decision** | incumbent seeded at `server.py:19040` `best_total = codegen_loop.score(errors) if existing else None`; per attempt `:19070` `attempt_score = codegen_loop.score(attempt_errors)`; `:19071` `if best_total is None or attempt_score < best_total:` → `:19072` `best_code, best_total = code, attempt_score`; final write-back `:19080` `if best_code is not None: write(name, best_code)` | strictly-better-on-total-project-errors, tier-ordered by `codegen_loop.score` (`codegen_loop.py:325`), where a masked/truncated measurement loses to an honest one |
| **Skip** | `server.py:19035` `if existing and not mine and not masked and build_state["exit_ok"]` | a file that already builds clean is never regenerated |
| **Abandon** | `server.py:19064` `if not build_state["ran"]:` → break | a build that did not launch is never scored |
| **Report** | `server.py:19088` `codegen_loop.format_report(rows, final, ok=not final and ran and exit_ok, ran=...)` | `ok` requires an empty error list **and** the build ran **and** it exited 0 |

## 2. Claims: confirmed vs refuted

### CLAIM A — "it discards its own result dict, so the verify step cannot influence what it keeps" → **REFUTED**

Already fixed on this branch's own history. `_codegen_build`'s docstring
(`server.py:18859`) records the fix: *"The second element is the build process's own
verdict, which used to be dropped here."* Every field of `workbench.run_program`'s dict
that bears on the verdict is consumed:

* `ok` → `build_state["exit_ok"]`, which gates the already-clean skip (`:19035`) and the
  final `ok=` (`:19090`), and synthesises an error line when the build failed but the
  caller's `error_regex` matched nothing (`:18978`).
* `timed_out` → an explicit `error: build timed out` line (`:18887`).
* `stdout_truncated` / `stderr_truncated` → `TRUNCATED_MEASUREMENT_NOTICE` (`:18891`),
  re-injected at `:18993` even when a stricter `error_regex` drops it. This is task #13/C1.

Verified by execution: the three existing loop tests in
`tests/test_codegen_build_loop_server.py` drive exactly these paths, and a probe run of
the whole tool with a failing build showed the verdict reaching the keep decision.
**Nothing to fix here. I did not invent one.**

### CLAIM B — "it is both the generator and the verifier" → **CONFIRMED (structurally), latent not live**

`codegen_build_loop` is the **only** tool listed in both role tables:
`grounded_outcomes.py:70` (inside `VERIFIERS`, `grounded_outcomes.py:62`, → `("compiled",
"failed")`) and `grounded_outcomes.py:79` (inside `GENERATORS`, `grounded_outcomes.py:77`). Measured, not derived:

```
OVERLAP generators&verifiers: ['codegen_build_loop']
self-grade via attribute(): {'attributed': True, 'interaction_id': 'iid-SELF',
  'signal': 'compiled', 'verifier': 'codegen_build_loop',
  'generator': 'codegen_build_loop', 'age_seconds': 0.0, 'recorded': True}
```

So at the module that owns the decision, a tool grading its own generation was
permitted with **no guard at all**, writing `compiled` (+0.70) into the same store
`4e2a315` had just finished de-prioritising self-graded rows in.

It was not *live* in production, for two reasons, and both are accidents:

1. The single call site `_feed_grounded_outcome` (`server.py:7718`) used `if` /
   **`elif`**, so the GENERATORS branch matched first and the VERIFIERS branch was
   unreachable for a dual-role tool. Measured: feeding
   `_feed_grounded_outcome("codegen_build_loop", True, ..., {"project": "p"})` with an
   eligible pending wrote nothing (`writes: []`, `attributed: 0`).
   This is exactly the shape `4e2a315` called out: *"harmless only while every call site
   happens to be is_good-gated, which is an unwritten contract the next caller cannot
   see."* The guard was in a different file from the decision.
2. `codegen_build_loop` never calls `_record_direct_tool` at all, so **neither** role
   fires for it today. It is nowhere near alone in that — see §7, **corrected**:
   `10 of 19 GENERATORS` and `3 of 11 VERIFIERS` never reach the recorder under their
   own name.

   ```
   codegen_build_loop  [G+V] def 18898  INERT
   build_run           [V  ] def  9994  live
   test_run            [V  ] def  9631  live
   file_write          [G  ] def  8739  live
   ```

Stated plainly: **the runtime is not currently marking its own homework here — but the
only thing stopping it is branch ordering in another module.** Fixed at the decision
site rather than left resting on that.

### CLAIM C (found while confirming B) — a failed outcome write silently burns the evidence → **CONFIRMED, live**

`attribute()` consumed the pending (`pending.judged.add(name)`) and incremented
`_STATS["attributed"]` **before** attempting the write. When `record_fn` raised, the
report said so — and **every caller discards the report** (`server.py:7743`). Measured:

```
failed write report: {'attributed': True, 'interaction_id': 'iid-B',
  'signal': 'compiled', 'verifier': 'build_run', 'generator': 'offload',
  'age_seconds': 0.0, 'recorded': False, 'error': 'db locked'}
```

Consequences: a locked database lost that grounded row permanently (the same verifier
kind can never claim that generation again — `test_one_verification_kind_judges_a_generation_only_once`
is the rule that makes it permanent), and `stats()["attributed"]` counted intent, not
rows. **This is the live form of "discards its own result dict"**, and it affects every
verifier (`build_run`, `test_run`, …), not just codegen.

## 3. Shrink floor — does one exist, and does it bind?

**Yes, and yes — verified end-to-end, not by reading.**
`codegen_loop.SHRINK_FLOOR = 0.75` (`codegen_loop.py:54`), enforced by
`shrink_rejected()` (`codegen_loop.py:437`), consulted by the loop at `server.py:19053`
before the write. Probe over the whole tool (1113-byte incumbent, model reply 2.2% of it,
failing build so the already-clean skip cannot mask it):

```
=== codegen build loop ===
  main.c                   attempt 2 rejected: shrank to 2% of the original
BUILD FAILED: 1 distinct error line(s)
--- file preserved: True
```

The floor was **unit-tested but not pinned through the tool**, and it has silently
no-opped once before (when `read()` pulled a key `file_ops.read_file` does not return,
`existing` was always `""` and `shrink_rejected("", ...)` returns `False`). Added an
end-to-end guard.

Note on that guard: my **first draft did not bind**. It used a 25-character reply, which
`shrink_rejected` rejects under its separate `len(candidate.strip()) < 40` near-empty
rule, so mutating `SHRINK_FLOOR` to `0.0` left the test green. Rewritten with a
273-byte / 24.5% reply plus in-test assertions that it clears 40 bytes and sits under the
floor. A guard that cannot fail is not a guard.

### The other two deletion-mode questions

* **Does "it compiles" get read as "it works"?** No. `format_report`
  (`codegen_loop.py:446`) prints at `codegen_loop.py:495`, on every green build: *"a green build is not proof the
  program works. A field that is declared and never assigned is not a compile error."*
  The tool docstring repeats it (`server.py:18929`).
* **Can a per-file repair spin on a cause in another file?** It cannot spin — `attempts`
  is bounded and scoring is on TOTAL project errors, so a rewrite of B that cannot fix a
  cause in A simply fails to beat the incumbent and B is left alone (`note: unchanged`).
  But it does **not escalate either**: nothing in the report says "no attempt on this
  file could move the project total, look elsewhere". Filed as a finding below, not
  fixed — outside this defect.

## 4. Fixes

Smallest sufficient change; the verifier is separated from the generator **at the module
that decides**, not by deleting a table entry.

1. `grounded_outcomes._candidate` (`grounded_outcomes.py:146`) — a pending generation
   produced by the verifying tool itself is skipped (`:162 if pending.tool == kind:`).
   `continue`, not bail-out: a tool's own row must not shadow an older row from a
   different generator that this verifier legitimately can judge. Returns
   `(pending, self_skipped)` so "nothing was waiting" and "the only thing waiting was my
   own work" stop looking identical; `attribute()` reports the second as
   `"<tool> may not grade the work it generated itself"`.
2. `grounded_outcomes.attribute` (`:173`) — a write that failed did not happen:
   `pending.judged.discard(name)` (`:223`) so a locked database no longer burns the
   evidence, and the retry lands. `_STATS` gains `recorded` (rows that landed, `:216`),
   `write_failed`, and `self_blocked`; `attributed` is documented as counting
   *decisions*, since it is incremented before the write and cannot be a row count.
3. `server._feed_grounded_outcome` (`:7718`) — `elif` → a second `if` (`:7742`), so both
   roles of a dual-role tool are consulted by role rather than by which membership test
   was written first. Safe only because of fix 1, and the comment says so.

Not done, deliberately: wiring `codegen_build_loop` into `_record_direct_tool`. That
would start writing grounded rows to the real store from a tool that currently writes
none — a feature addition with production side-effects, outside "smallest sufficient
change" and outside a session forbidden from touching the operator's store. Filed as
Important below.

## 5. Tests — RED before GREEN

Scoped files run (never the full suite): `tests/test_grounded_outcomes.py`,
`tests/test_codegen_build_loop_server.py`, `tests/test_codegen_loop.py`.

Baseline at the parent, same three files, before any test was added:

```
79 passed in 17.70s
```

(The re-review reproduced this baseline at `79 passed in 1.88s`, and `2.31s` on a
deliberately cold cache. The 17.70s was cold worktree file cache on `D:` plus sibling
agents on a 16 GB box, not work skipped in the faster runs — item counts, not the clock,
are what prove both runs reached the same stage, and 79/87 reconcile exactly.)

**RED** — new tests against the parent's `grounded_outcomes.py` and `server.py`
(restored with `git checkout HEAD -- <paths>`; **no `git stash` was used at any point**):

```
5 failed, 82 passed in 1.77s
```

Every failure is behavioural (`AssertionError`), never an import/attribute/fixture error:

| Test | Parent failure message |
|---|---|
| `test_a_tool_never_grades_the_work_it_generated_itself` | `assert True is False` |
| `test_a_self_generated_row_does_not_hide_an_eligible_older_one` | `AssertionError: assert [('newer', 'compiled')] == [('older', 'compiled')]` |
| `test_a_failed_write_leaves_the_generation_still_judgeable` | `assert False is True` |
| `test_stats_count_rows_written_apart_from_attribution_decisions` | `AssertionError: assert None == 1` |
| `test_a_dual_role_tool_is_routed_by_role_not_by_branch_order` | `AssertionError: assert [] == [('gen-1', 'compiled')]` |

**GREEN** — same three files, same stage (full collection, no early abort):

```
87 passed in 1.63s
```

79 → 87 is the 8 tests added (6 in `test_grounded_outcomes.py`, 2 in
`test_codegen_build_loop_server.py`). Both runs collected and executed the same three
files, so the counts are comparable. Adjacent server tests that monkeypatch the same
recorder were also run: `tests/test_eval_history.py tests/test_inspection_facade.py` →
`27 passed in 1.52s`.

Three of the new tests are characterization/coverage rather than RED→GREEN, and are
labelled as such: `test_the_two_role_sets_overlap_on_exactly_one_tool`,
`test_another_verifier_may_still_judge_a_codegen_generation`, and
`test_a_shrinking_regeneration_never_replaces_the_file_on_disk`.

### 5b. Round two — closing the re-review's condition

The re-review found `test_a_dual_role_tool_is_routed_by_role_not_by_branch_order`
**narrower than its name**: its fixture output carried no `[interaction_id: ...]`, so the
GENERATORS branch was a silent no-op and the test proved only that the verifier branch
runs — never that it runs *safely* alongside the generator branch it now shares a call
with. Confirmed: the old version **survived M1 unchanged**. That is the same error as the
first shrink-floor draft — exercising a neighbouring, safer path than the one the change
made reachable.

Widened: the output now carries `[interaction_id: gen-self]`, and the test asserts
`go.pending_count() == 2` before the verdict, so it fails loudly if the tag ever stops
matching `_INTERACTION_ID_RE` rather than quietly reverting to testing half the call
site. RED under mutation, verbatim:

| mutation | result | the widened test's message |
|---|---|---|
| M1 self-guard removed | `3 failed, 84 passed in 1.46s` (was 2 failed) | `AssertionError: assert [('gen-self', 'compiled')] == [('gen-1', 'compiled')]` |
| M3 `elif` restored | `1 failed, 86 passed in 1.59s` | `AssertionError: assert [] == [('gen-1', 'compiled')]` |

Under M1 the tool writes `compiled` (+0.70) **against its own interaction id** — the
exact call-site self-grade the re-review proved reachable and that nothing in the suite
caught. It now fails under both mutations: M1 shows the guard is load-bearing, M3 shows
the routing fix is.

The two Minors, both taken:

* **`unlinked` blended self-blocks with "nothing was waiting".** Fixed
  (`grounded_outcomes.py:186`): the two are counted apart rather than summed and split
  later. RED verbatim before the fix — `1 failed, 86 passed in 1.51s`,
  `assert 1 == 0` at `tests/test_grounded_outcomes.py:245`. New mutation **M6**
  (re-blend them) → `1 failed, 86 passed`, so the split binds.
* **`test_stats_count_rows_written_apart_from_attribution_decisions` never asserted
  `attributed == 2`,** the very split it is named for. Assertion added. It was *already
  true*, so this is coverage rather than a fix, and it is reported as such — the
  under-assertion was the defect, not the counter.

## 6. Do the guards bind? Mutation results

Each fix reverted individually, in place, with the scoped suite re-run and the file
restored afterwards:

| Mutation | Result |
|---|---|
| M1 remove the self-grading guard | **`3 failed, 84 passed`** (2 before the test was widened) — `test_a_tool_never_grades_the_work_it_generated_itself`, `test_a_self_generated_row_does_not_hide_an_eligible_older_one`, `test_a_dual_role_tool_is_routed_by_role_not_by_branch_order` |
| M2 remove `pending.judged.discard(name)` on a failed write | `2 failed, 85 passed` — `test_a_failed_write_leaves_the_generation_still_judgeable`, `test_stats_count_rows_written_apart_from_attribution_decisions` |
| M3 restore `elif` at the call site | `1 failed, 86 passed` — `test_a_dual_role_tool_is_routed_by_role_not_by_branch_order` |
| M4 `SHRINK_FLOOR = 0.75` → `0.0` | `2 failed, 85 passed` — `test_a_shrinking_regeneration_never_replaces_the_file_on_disk` (new, end-to-end), `test_shrink_rejects_an_amputation` (existing unit) |
| M5 (added by the re-review) `continue` → `break` in the guard | `1 failed, 86 passed` — `test_a_self_generated_row_does_not_hide_an_eligible_older_one` |
| M6 re-blend `unlinked` and `self_blocked` | `1 failed, 86 passed` — `test_a_tool_never_grades_the_work_it_generated_itself` |

All six bind. M4 caught my own non-binding first draft of the shrink guard before this
table was written; **M1 caught the second instance of the same error** — the call-site
test passed under it until the re-review flagged it and it was widened. Twice in one
lane, the same failure mode: a test that exercises the neighbouring safe path instead of
the newly reachable dangerous one. Mutation is what found it both times; reading the test
did not.

## 7. New findings (not fixed here)

* **CRITICAL — the attribution machinery is very close to entirely dead in production.**
  This entry replaces a claim in the first version of this report that was **measurably
  false**: *"Every other name in both tables either reaches the recorder or is a verifier
  that does."* Re-measured from the AST, on the criterion that actually governs — is
  `_record_direct_tool` ever called with **this tool's own name** as its first argument
  (a transitive call through a differently-named tool records under *that* name, and
  `_record_direct_tool` early-returns on `activity_tracker.inside_tool_call()` anyway,
  `activity_tracker.py:648`):

  | table | live | inert |
  |---|---|---|
  | `GENERATORS` (19) | 9 | **10** — `agent`, `apply_learned`, `codegen_build_loop`, `consult`, `ensemble_answer`, `improve_function`, `offload`, `scaffold_project`, `sonder`, `workbench_agent` |
  | `VERIFIERS` (11) | 8 | **3** — `artifact_verify`, `codegen_build_loop`, `ground_artifact` |

  My first pass reported 7/19 because it followed transitive call paths
  (`sonder → control_command → directory_tree → _record_direct_tool`). Those paths are
  real but irrelevant: they record under `directory_tree`, which is in neither table.
  Corrected number is **10/19**, matching the re-review.

  **The production consequence, stated plainly.** `note_generation` fires only when a
  tool clears *two* gates: it reaches the recorder under its own name, **and** its output
  matches `_INTERACTION_ID_RE` (`server.py:7704`). Measured, the only producers of that
  footer are `with_footer` (`server.py:558`), `_sonder_impl_serialized`, `_offload_impl`
  and `_answer_with_history_impl` — i.e. the learning-tier tools. **Every single one of
  them is in the inert column.** The 9 live generators are the file/patch/artifact tools,
  whose output is a diff or a byte count and carries no footer at all. The two sets do
  not intersect: transitive search finds exactly one live generator that can even
  *embed* a footer, `game_generate_and_test`, and only via an internal `sonder` call
  (`game_generate_and_test → _game_generate_result → sonder → _sonder_impl →
  _sonder_impl_serialized`).

  So: **`note_generation` essentially never fires, therefore `attribute` essentially
  never has anything to judge, therefore the eight live verifiers are writing grounded
  outcome rows at close to a rate of zero.** The module was built to fix a 8,883-vs-192
  reporting bias and is not currently moving that ratio. Do not read a coverage figure
  off these tables — the tables describe intent, not wiring.

  This is pre-existing and is not introduced by this change. Two honest consequences,
  in both directions: it **lowers the live severity of defect #35** (the self-grade could
  not fire because nothing fires), and it **raises the value of the wiring work this
  report deferred** far above the value of the guard itself. The guard is what makes that
  wiring safe to do; it is not, on its own, worth much until the wiring lands.
  I have not fixed the wiring here — it would start real writes to the operator's outcome
  store from ten tools at once, which is out of scope for this session by instruction.
* **Minor — a per-file repair whose cause is in another file is silent.** The loop
  correctly declines to keep a worse file (`note: unchanged`) but never says "no attempt
  on this file moved the project total; the cause is elsewhere". A caller reading
  `unchanged` cannot tell "the model could not improve it" from "this file is not where
  the problem is".
* **Confirmed-good, worth keeping visible:** the truncation, unrun-build, error-cap,
  parse-phase and declare-phase masking guards in `codegen_loop.py` are the densest
  floor-vs-total defences in this repo, and `format_report` refuses to print a bare
  number when any of them fire.

## Provenance

Investigated and fixed 2026-08-10/11 on `work/15-codegen-loop` @ parent `4e2a315`;
revised 2026-08-11 to close the re-review's condition (`rereview-codegen.md`, verdict
MERGE) -- the widened call-site test, both Minors, and the corrected inertness
measurement in Sec. 7.
Every number in this report was measured by running the code, not derived. No `git
stash`, no `git add -A`, no live benchmark or codegen campaign, no writes to the
operator's store; vendored `app/build/**/local-system/*.py` untouched.
