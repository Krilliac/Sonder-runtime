# Attribution residuals: #58, #57, #56

Branch `work/28-attribution-residuals`, base `06c2f79`. Three commits, clean
checkout, nothing pushed. No `git stash`, no `git add -A`, no sibling worktree
touched. `scripts/select_regression_tests.py` was **not** used; files are named
deliberately below.

## Lineage — verified, and one brief figure was wrong

`git merge-base --is-ancestor <b> HEAD`, run in this worktree:

| branch | ancestor of HEAD? |
|---|---|
| `feat/verified-fetch-modes-calibration` (`9f377f1`) | **yes** |
| `work/27-sweep-fallout` | **yes** — and it points at `06c2f79`, i.e. HEAD exactly |
| `main` (`f018265`) | yes (merge-base `f018265`) |
| `fix/runner-infra-evidence` | **no** — see #56 |
| `work/17-selfmod-gate` | **no** — see #57 |
| `work/25-login-grading` | **no** — see #57 |

All four inherited fixes confirmed by reading the code, not the brief:

- non-degradable `UNCLASSIFIED` — `permission_modes.py:271`, all four `_MATRIX`
  rows, and `decide()`'s no-degrade branch (`if risk == UNCLASSIFIED`).
- root confinement — `harness_tools._resolve_root` calls
  `_require_authorized_root` on every entry point.
- `_resolve_target_path` — `harness_tools.py:88`, applied at four call sites
  (348, 477, 501, 529), raising `path is outside the root it was given`.
- S8 authorize-before-stat — `_require_authorized_root(p)` precedes `p.is_dir()`
  and the unauthorized refusal names no path.

**Brief figure that does not hold: "#56 — `attribute(evidence=)` reaches 4 of 11
VERIFIERS."** On this lineage `attribute` has **no `evidence` parameter at all**
— measured signature `(tool, ok, project='', record_fn=None, run_id='')`. So it
reached **0 of 11**, not 4. The `evidence=` API exists on
`fix/runner-infra-evidence`, which is not an ancestor of HEAD; there it is wired
at **7** of 11 tool call sites (the four harness verifiers plus
run_code/run_project/isolated_run), not 4. Mine governs: 0 of 11 here.

---

## #58 — `verification_ok` for a command that examined nothing

**Reproduced**, end to end, before any edit, on a project whose only content is
one changed file and which has no build system at all:

```
_agent_verification_covers("build_run", {"root": proj, "command": "git --version"},
                           [<proj/payments.py>])   -> True
_agent_verification_covers(..., command="python -c pass")                -> True
_agent_verification_covers(..., command="")                             -> True
harness_tools.build_run(root=proj, command="git --version")
   -> ok=True  returncode=0  stdout='git version 2.45.2.windows.1'
autopilot_controller._task_passed(HostTaskResult(validation_passed=True),
                                  {"kind": "validate"})   -> (True, '')
```

For contrast, the S2 fix does bind where it can reach: `test_run` with
`path="tests"` returns False. It cannot reach `build_run` because
`harness_tools.build_run`'s signature is `(root, command, timeout, extra_roots)`
— there is no `path`.

**Changed.** `_agent_build_command_examines` in `server.py`, called from
`_agent_verification_covers` for `build_run` only. The control is the one
`_agent_validation_covers` already applies to `workspace_run` — the sibling tool
whose whole argv the caller also chooses — on the *validation* route: refuse
self-reporting flags, refuse clean-only, refuse inline `-c`/`-e`, else demand a
recognized build/test action or explicit path targets covering the change. The
analogue of "a receipt the prober cannot fabricate" is the **empty `command`**:
the argv is then derived from the root's own marker files, which the caller
cannot forge, and a root with no build system returns `ok=False` so `tool_ok`
already refuses. `_AGENT_BUILD_DRIVERS` is derived, not recalled — its first rows
are literally the argv `harness_tools.build_run` auto-detects, the rest are the
programs `_agent_validation_covers` already treats as broad. Tokenized with
`str.split()` because that is exactly what the child does.

**Deliberately not done.** Did not touch the other ten verifiers (they have a
`path`, or no free-form argv). Did not extend `_agent_validation_covers`'s shared
table — widening it would relax `workspace_run`'s gate, outside this brief. Did
not stat the model-supplied `root` to detect its build system: that would
reintroduce exactly the S8 existence oracle this lineage just closed.

## #57 — `dangerous` is degradable; ruling on which grades are not

**Reproduced.** `decide(interactive=False)`, no operator rules:

```
tool                   risk        plan   manual  acceptEdits  auto
admin_login            ask         deny   allow   allow        allow
admin_set_account      dangerous   deny   allow   allow        allow
admin_register         dangerous   deny   allow   allow        allow
elevate                dangerous   deny   allow   allow        allow
permission_rule_set    dangerous   deny   allow   allow        allow
file_delete/git_merge/sqlite_mutate/self_heal_repair … all the same
```

**Ruling: a narrow declared class, not the whole grade.**
`permission_modes.DURABLE_AUTHORITY_TOOLS = {admin_login, admin_register,
admin_set_account, elevate, permission_rule_set}` is exempt from the
non-interactive degrade (new `decide()` step 5b, `source="durable-authority"`).

**What making all of `dangerous` non-degradable would break — measured.** The
catalog grades **19** commands `dangerous`; **8** are reachable from
`_agent_dispatch`: `file_delete`, `git_cherry_pick`, `git_merge`,
`memory_privacy_repair`, `memory_quality_repair`, `self_heal_repair`,
`sqlite_mutate`, `task_delete`. A class-wide rule refuses all eight in every
non-interactive lane — the agent and autopilot lanes entirely. That is a
shutdown, not a gate. **The answer is "too much", so the narrower change is what
shipped**, drawn where the degrade's own justification stops: it trades an
unanswerable prompt for "this can be undone", which for privilege means "the
operator can revoke it later" and assumes the operator *knows it happened* — and
the degraded prompt is precisely the notice that never arrived.

**What the narrow class breaks — measured: nothing in the agent lane.** 0 of the
5 are in `tool_capabilities.dispatch_names(server._agent_dispatch)`. What binds
is the MCP protocol entry point and the HTTP one. Both routes out survive and are
tested: a console operator arrives with `interactive=True` and is *asked*; an
explicit `allow` rule still satisfies the ask at step 3.

`permission_rule_set` is a member because without it the class does not bind —
verified: step 3 resolves an ALLOW rule *before* the degrade, so a caller who can
write rules unattended writes one for `admin_register` and walks through.

**Deliberately not done.** `/selfmod` is **not** added: `work/17-selfmod-gate`
(`39d78ca`) gates it with a per-invocation `non_degrading=` flag keyed on the
*action*, because `/selfmod status` and `/selfmod deploy` share an entry point.
Right shape there, wrong shape here; the two compose. `runtime_policy_update` is
left out — it is policy, not authority; a candidate for a later argument.
`admin_login` is in the class but on this lineage the catalog grades it `ask`,
which acceptEdits/auto ALLOW *before* the degrade is reached, so the class cannot
bind it in those two modes. Regrading it is `work/25-login-grading`'s `92e177c`
(199 lines in `command_catalog.py`, with its own measured justification);
duplicating that here would put two rival versions into the merge. Recorded as a
test that flips automatically when that lane lands.

## #56 — infrastructure failure scored as `failed`

**Reproduced**, and worse than filed:

```
note_generation("gen-1","sonder",project="p"); pending=1
attribute("build_run", ok=False, project="p", record_fn=…)
  -> {"attributed": True, "signal": "failed", "recorded": True}   reward -1.0
a LATER real passing build_run for the same generation
  -> {"attributed": False, "reason": "no recent generation to judge"}
```

The blip does not merely add a wrong row — it **burns the one-shot entry**, so
the genuine verdict is permanently displaced. Measured harness returns: no build
system / unknown framework / unknown linter carry `error` with no process
spawned; command-not-found and timeout carry `returncode: -1`; a real failure
carries the child's integer status. Only the last is about the work.

**Changed.** `evaluation_infrastructure_error` (dict) and
`rendered_infrastructure_error` (text) in `grounded_outcomes`; `attribute` gains
`evidence=` and refuses *before* claiming a pending entry; `_STATS["unmeasured"]`.
Server: `_feed_grounded_outcome`/`_record_direct_tool` gain `evidence=`, wired at
both branches of the four harness verifiers; `_format_run_result` stopped
dropping `error` (a model was being told `ok: False` with no reason). The text
reader stops at the first `stdout:`/`stderr:` header so a failing suite's own
output can never be read as an infrastructure report — losing a real negative is
the worse mistake here. The two predicates are pinned against each other over the
same measured dicts.

**`note_generation` inertness — confirmed statically, and it changes the claim.**
The `[interaction_id: …]` footer is produced only by `_offload_impl`,
`_sonder_impl_serialized`, `_answer_with_history_impl`; there is **no**
`_record_direct_tool("sonder"/"offload"/"agent"/"consult"/…)` call anywhere, and
the generators that *do* record (`file_write`, `text_patch`, `artifact_generate`,
…) never emit a footer. The two gates are disjoint on both paths. So the brief's
"the ones that are written get skewed toward −1.0 by this path" **is not
supportable: this path writes nothing today.** The fix is pre-emptive and
correct — the moment generators are wired it would start writing −1.0s — but no
production harm figure should be quoted for it. Generators were **not** wired up,
per instruction.

**Deliberately not done.** `run_code`/`run_project`/`isolated_run`/
`codegen_build_loop` and the three artifact verifiers keep dict-free behaviour
(covered only by the text reader). `fix/runner-infra-evidence` already has a
measured, `code_runner`-specific predicate for that family — *`error` beside an
integer returncode is a verdict; `error` without one is the runner* — and
duplicating it worse here would put two rival versions into the merge.

## `lesson_quarantine` — SEPARATE, and the reason is measured

`retriever.lesson_quarantine` reads `avg_reward_since_win`, computed in
`sonder_runtime/adapters/memory_store.lesson_usage_stats` from
`lesson_usage_history`, whose SQL is `SELECT … FROM lesson_usage WHERE reward IS
NOT NULL` — confirmed, **no `outcome_signal` filter**. But it is fed by a
different writer: `record_lesson_usage_outcome`, reached only from
`record_outcome_and_claim_lesson_distillation` (the operator-facing
`record_outcome` tool). `attribute`'s writes go through
`server._record_outcome_signal` → `memory_store.record_outcome_row`, which
touches the `outcomes` table **only**. So none of the −1.0s this task fixes ever
reach the quarantine gate. Different table, different writer, disjoint. Folding
it in would also mean deciding which signal populations count as lesson evidence
— the de-blending design question — and changing live lesson eviction. **Separate
item.**

## Mutation results — every guard planted, observed failing, reverted

| # | guard | mutation | result |
|---|---|---|---|
| 58 | command check call site | `if False and …` | 11 failed, 18 passed |
| 58 | no-op flag check | `if False and …` | 1 failed, 28 passed |
| 57 | `DURABLE_AUTHORITY_TOOLS` branch | `if False and …` | 14 failed, 18 passed |
| 57 | drop `permission_rule_set` from the class | member removed | 4 failed, 28 passed |
| 56 | infrastructure refusal | `if False and …` | 4 failed, 37 passed |
| 56 | output-header stop in the text reader | `if False and …` | 1 failed, 40 passed |
| 56 | rendered-verdict read | `if False and …` | 6 failed, 35 passed |

## Verbatim pytest lines

RED, at the final item count:

```
#58  11 failed, 18 passed in 1.27s   tests/test_verification_examines_work.py
#57  17 failed, 15 passed in 1.45s   tests/test_permission_durable_authority.py
#56  27 failed,  1 passed in 1.43s   tests/test_grounded_outcomes_infrastructure.py
#56   7 failed,  6 passed in 2.03s   tests/test_grounded_outcomes_agent_dispatch.py (repaired fixtures)
```

GREEN, final, 26 named files:

```
1054 passed, 7 skipped in 126.94s (0:02:06)
```

`test_verification_examines_work test_permission_durable_authority
test_grounded_outcomes_infrastructure test_agent_verification_gate
test_agent_dispatch_dev_tools test_autopilot_controller test_autopilot_server
test_harness_build_diff test_harness_dev test_harness_misc test_harness_git
test_harness_root_confinement test_permission_modes test_permission_gate_coverage
test_permission_gate_dispatch test_permission_gate_http
test_permission_policy_display test_permission_rules test_reloadable_mcp
test_risk_of_fail_closed test_command_router_catalog test_grounded_outcomes
test_grounded_outcomes_agent_dispatch test_learning_health test_memory_store
test_selfmod`

The full suite (~522s) was not run.

## Commits

```
5ec09dc  A verifier that named nothing verified nothing (#58)
f1adfac  Granting authority is the one thing "nobody to ask" must not mean yes (#57)
604e662  A verifier that could not run measured nothing (#56)
```

## NEW findings

**Critical — a FAILING verification is filed as a PASS on the agent path.** Found
while fixing #56 and strictly worse than it.
`server._agent_dispatch_observed` computes `ok = not
str(observation).startswith("ERROR:")` — a statement about the dispatcher, not
about the work. Measured by execution on a real project with one failing test:

```
harness ok=False returncode=1
rendered first line: 'test run (pytest)'
agent-path ok -> True          signal that would be recorded: tests_passed  (+1.0)
```

And measured on the four harness verifiers and `run_code`, `ERROR:` is emitted
*only* when the tool never ran (their `except` branch; a `ValueError` out of
`code_runner`). So on the agent path, before this fix, **every** `failed` row was
an infrastructure failure and **every** real failure was recorded as a pass. A
manufactured success in a store whose caller-judged population sits near 52% is
indistinguishable from real progress. Fixed here (`grounded_outcomes.
rendered_verdict`, read at the outcome feed only, so activity logging and other
consumers of `ok` are untouched) — but the `ok` derivation itself is unchanged
and still feeds `activity_tracker.record_tool_result`, so any *other* consumer of
that flag inherits the same inversion. Not audited; filed.

**Important — five tests encoded the defect as the requirement (the 10th case in
this repo).** `tests/test_grounded_outcomes_agent_dispatch.py` stubbed a failing
verification as `"ERROR: build failed"` / `"ERROR: it does not compile"`, and one
docstring asserted outright that "a tool that … returns its own `ERROR: …` string
is still a real verdict". Measured, that is false for every member of
`VERIFIERS`. Read before touching; every assertion kept, only the stand-in
replaced with the measured `code_runner.format_result` shape, plus two new tests
for the two halves of the distinction.

**Important — `_record_outcome_signal` bypasses the lesson-usage update.** It
calls `memory_store.record_outcome_row` directly ("bypassing the model-facing
wrapper"), while the operator-facing `record_outcome` goes through
`record_outcome_and_claim_lesson_distillation`, which *also* updates
`lesson_usage.outcome_signal`/`reward` and claims lesson distillation. So every
auto-attributed outcome is invisible to lesson quarantine and never distils a
lesson. Whether that asymmetry is intended is undocumented — the docstring
mentions only the model-facing wrapper, not the two writes it drops.

**Important — a class-wide non-degrade would have been a shutdown.** Recording
the number so a later lane does not re-derive it: 19 `dangerous` commands, 8
agent-dispatchable. `tests/test_permission_gate_dispatch.py::
test_manual_refuses_nothing_the_mode_did_not_refuse_before` is the floor that
catches this; it broke on my change, does **not** encode a defect, and was
widened by the enumerated class only, plus a new assertion that the class is
disjoint from `_agent_dispatch` so the cost claim is checked rather than argued.

**Merge hazard — three lanes contend for the same two functions.**
`fix/runner-infra-evidence` has its own `attribute(evidence=)` in
`grounded_outcomes.py` (without this lineage's `run_id`), and `work/17-selfmod-
gate` adds `non_degrading=` inside the exact `if action == ASK and not
interactive:` block this lane's step 5b now occupies. Both are semantic
complements of what landed here, not alternatives; a "take theirs" resolution on
either file silently drops one side. `work/25-login-grading` contends for
`command_catalog._DANGEROUS`.
