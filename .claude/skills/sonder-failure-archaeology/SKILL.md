---
name: sonder-failure-archaeology
description: >-
  Chronological institutional memory for the Sonder Runtime repo: every major
  investigation, dead end, rejected fix, and revert, each with symptom, root
  cause, evidence, and current status. TRIGGER when the user asks "why was this
  reverted", "has this been tried before", "did we already fix this", "what
  happened with the smoke gate", "why is this check written this way", "what's
  the history of this bug", or before re-attempting a fix that a comment or
  report hints was tried. DO NOT TRIGGER for diagnosing a live failure happening
  right now — that is symptomatic what-to-try-next work; use
  sonder-debugging-playbook instead. For commit/merge/ratchet rules, use
  sonder-change-control.
---

# Sonder failure archaeology

The recorded history of what broke in the Sonder Runtime, why each fix took
the shape it did, and which defects are still open. Code review shows the scar
tissue; this file is the wounds.

Each incident is **Symptom / Root cause / Evidence / Status today**. "Status
today" was re-verified against commit `99162cf9` on 2026-08-22 by reading the
current tree, not by trusting the reports. Primary sources: the investigation
reports under `.superpowers/sdd/work/*.md` (in-repo) and the commits named per
incident. Status values: **fixed** (verified in the tree at `99162cf9`),
**open** (confirmed still present), **policy** (a standing decision — do not
casually undo).

**When NOT to use this skill:** you have a failing test, stack trace, or red
gate *right now* and want to know what to try — use `sonder-debugging-playbook`
(symptomatic, present-tense). Use this skill when the question is historical:
was this tried, why is this guard shaped strangely, why did a capability
disappear.

---

## Incident index

| # | Symptom in one line | Status |
|---|---|---|
| 1 | Selfmod smoke gate was a constant-true assertion; broken candidate auto-approved | fixed |
| 2 | `required_kinds` verification compared a list with itself; absent artifacts passed | fixed |
| 3 | Seven nights of self-improvement recorded data, changed zero lines of code | fixed |
| 4 | Error-signal ratchet went RED; the tempting fix was regenerating the baseline | fixed |
| 5 | `shell_run` arbitrary-shell tool shipped, reverted wholesale same day | policy (revert stands) |
| 6 | ask→allow degradation let `/selfmod deploy` through unattended | fixed |
| 7 | Advertised tools with no dispatch branch; gate-dead vocabulary behind them | fixed |
| 8a | `secret_scan` counts unreadable files as scanned | **open** |
| 8b | `reclaim_orphans` counts cancel failures as successes | **open** |
| 8c | Regression selector diffed the tree against itself; text-match selection | fixed |
| 9 | Guards green on arrival: holes encoded as tests, bounds that never engage, narrow doubles | partially fixed (flavor 3 open repo-wide) |
| 10 | Counting loop swallowed every raise, reported a measured-looking zero | fixed |
| 11 | Fail-closed guard bypassed by a broader except two lines below it | fixed |
| 12 | Cross-branch half-fixes; figures measured at one revision quoted at another | fixed (discipline standing) |
| 13 | Selfmod guard series: duplicate guard guarded nothing; unsatisfiable review discarded | fixed |

---

## The meta-pattern (read this first)

Two sentences recur across every report in `.superpowers/sdd/work/`:

1. **"A check that stops checking is indistinguishable from a check that
   passes."** A constant-true assertion, a swallowed exception, a scan of zero
   files — all fabricate the reassuring value. Incidents 1, 2, 8, 9, 10 and 13
   are this defect in different clothes.
2. **"A figure measured at one revision reported as a fact about another."**
   Counts carried forward until false; drifted file:line anchors; a lane
   quoting a selector figure its lineage could not have produced. Incident 12.

---

## Incident 1 — the fake smoke gate (the founding incident)

**Symptom.** `selfmod` — the subsystem that lets the runtime rewrite its own
source — required a passing `smoke` check before approving any candidate. A
candidate whose entry point raised `NameError` at import reached
`phase=approved`, `approved_by='host:auto-low-risk'`, no operator present.

**Root cause.** The smoke check was
`python -c "import pathlib; assert pathlib.Path('.').is_dir(); print('selfmod smoke ok')"`
run with the candidate workspace as cwd. `'.'` is always a directory; the
assertion was a constant. It never read one byte of the candidate — and no
other gate covered the gap: `py_compile` proves a parse, never module-level
execution; pytest only exercises what tests import.

**Fix shape.** Made real rather than removed, because nothing else covered
what it named. `selfmod.record_smoke` imports every declared module in a
**child process** rooted at the workspace, confirms declared deletions are
genuinely gone, and must return a SHA-256 receipt over the bytes it actually
loaded. The expected receipt is computed by the *recording* process from disk
and never handed to the probe — a stubbed probe exiting 0, or one printing a
forged receipt, is refused. Exit status is not evidence a check ran.

**Sibling found the same day.** The required `syntax` check degraded to
`print('no Python syntax targets')` when the `.is_file()` filter emptied its
target list — which happens exactly when the change *deleted* its declared
modules, the shape repair loops converge on (Incident 3). An empty target set
is now a refusal; deletion-only runs go to explicit human review.

**Evidence.** Commit `7bdfa19f` (2026-08-11); `smoke-gate-report.md` (includes
the plant-and-revert table: stubbed probe REFUSED, forged receipt REFUSED,
sound candidate PASSES).

**Status today: fixed.** `selfmod.py` `smoke_plan` (886), `record_smoke`
(915); `server.py:2195` `_selfmod_test_commands` no longer builds a smoke
command (comment at 2226 records why); `server.py:2351` calls `record_smoke`.

---

## Incident 2 — tautological verification (`required_kinds`)

**Symptom.** `assetgen.verify_pack` asked artifact grounding to enforce
`required_kinds`; a kind whose writer silently produced nothing still passed.

**Root cause.** Both sides read `manifest["kinds"]` — the *requested* kinds —
so `kind in set(manifest["kinds"])` compared a list with itself: corruption
was caught, absence was not. Found while fixing: the whole loop sat inside
`if isinstance(manifest, dict)`, so a bundle with **no manifest** had the
requirement silently dropped and reported `ok`.

**Fix shape.** Verification needs two independent sides. `assetgen.KIND_ARTIFACTS`
supplies what each kind must leave *on disk*; the check compares requested
kinds against files actually found. A required kind with no evidence map is a
refusal — a check that cannot look must not report success — and the loop was
hoisted out of the manifest guard.

**Evidence.** Commit `c1c1475e`; `smoke-gate-report.md` §3-4 (plant: 'icon'
requested, nothing produced → REFUSED).

**Status today: fixed.** `assetgen.py:84` defines `KIND_ARTIFACTS`; line 934
passes `kind_files` evidence to the check.

---

## Incident 3 — seven nights of nothing

**Symptom.** Seven nights of the self-improvement cycle recorded 4,213
interactions and 3,959 outcomes into the learning stores and changed **zero
lines of code**.

**Root cause.** The deploy machinery (worktree, candidate, tests, commit,
backup, rollback) existed and worked; nothing ever invoked it. Five selfmod
runs existed, all hand-created, none deployed — one named a real defect
("permission_rules.load silently degrades to default rules") later fixed
independently by a human: the loop was finding things and dropping them.
Improvement loops that never land changes are activity theater.

**Fix shape.** `scripts/nightly_selfmod.py` drives one full lifecycle per
night, each guard justified by an in-repo measurement: honours the configured
mode (default `propose` stops at `reviewing`; only `auto-low-risk` deploys
unattended); refuses a dirty tree; a 75% shrink floor because unguarded repair
converges on deletion (measured: 44% and 13% of a file returned to fix two
typos); a model's error *string* is an abort, never a candidate; runs the
whole suite; never proposes against `server.py`.

**Evidence.** Commit `cee18669` (2026-08-08). The same commit adds
`scripts/run-tests.cmd` after a venv quoting failure (`No Python at
'"C:\...python.exe'` — stray quote) was misdiagnosed twice and cost a lane its
entire verification step.

**Status today: fixed.** Both scripts present at HEAD. See Incident 8b for a
counting bug still open inside the nightly script.

---

## Incident 4 — the ratchet temptation

**Symptom.** `scripts/check_error_signals.py` — the shrink-only baseline over
stringly `ERROR:` signals — went RED after commit `2cec3272` added a new
literal-prefixed refusal to `_agent_dispatch`. The one-line cheat was to
regenerate the baseline, tempting because the new refusal was *correct*. The
repo's rule (see `sonder-change-control`): the baseline may only shrink;
migrate the site, never regenerate.

**What was done instead.** The refusal moved into a gate helper
(`_agent_project_root_refusal`) that `_agent_dispatch` forwards, matching the
documented idiom. Before ruling the pinning test bureaucratic, the lane
**measured the bypass consequence**: with only that lock emptied,
`secret_scan(root=".")` on a read-only rootless agent run returned **32
findings in 924 files scanned** — including a live `admin_auth.py` dev secret
— because the second lock (`harness_tools._resolve_root`) does not refuse
`root="."`. The message string is the only observable saying which lock
answered; neither the lock nor its assertion can be deleted.

**Evidence.** Commit `1ab0038b`; `ratchet-selector-report.md` Item 1 (the
32-finding measurement, plant-and-revert with unpiped exit codes);
`ratchet-doubles-report.md` (cherry-pick to a second lineage; also its
Critical: the ratchet counts *syntax*, not signals — `ERROR:`-prefixed
assignments and membership tests are invisible to it).

**Status today: fixed.** `python scripts/check_error_signals.py` exits **0**
at `99162cf9` (run 2026-08-22). `_agent_project_root_refusal` is at
`server.py:16588`.

---

## Incident 5 — the safety revert (`shell_run`)

**Symptom.** Feature commit `8fa937cc` ("feat: shell_run tool + opt-in test
scaffolds; harden consult/code_improve/router") was reverted wholesale the
same day rather than patched.

**Root cause / policy.** `shell_run` was an arbitrary-shell tool on an AI
runtime. The demonstrated stance: capability-expanding surfaces judged unsafe
get **wholesale reverts, not in-place patches**. 612 lines removed across 10
files; merged to `main` as a safety revert.

**Evidence.** Commit `ae9503b0` (2026-08-08); merge `c60ba932` (2026-08-08).

**Status today: policy, revert stands.** `shell_run` has zero occurrences in
`server.py` at HEAD. Do not reintroduce an arbitrary-shell tool; the
argv-checked paths (`workspace_run` / `script_run`) are the sanctioned route.

---

## Incident 6 — ask→allow degradation reached `/selfmod deploy`

**Symptom.** `/selfmod deploy|rollback` was graded `dangerous` by the
permission catalog and let through anyway: **12 of 16 mode/action/surface
combinations reached the write path** (only `plan` refused).

**Root cause.** The gate maps `dangerous` → `ask`, and every surface reaching
`_selfmod_command` decides with `interactive=False`, where `ask` degrades to
`allow`. The degrade is deliberate for ordinary tools — an unanswerable prompt
resolves to yes because the result can be undone — but `selfmod.deploy`
`os.replace`s Sonder's own source: the one operation that can overwrite the
interpreter that would perform the recovery.

**Fix shape.** `permission_modes.decide(..., non_degrading=True)` —
per-invocation, **keyed on the action, not the command** (`/selfmod status`
arrives at the same entry point and must not be refused unattended). Two
escape routes survive, both tested: a console operator who answers, and an
explicit allow rule. Mutation testing found a guard nobody held — hardcoding
`operator_approved=True` in the repl passed all 28 tests; an AST check on
`sonder_repl` now pins the wiring.

**Evidence.** `selfmod-gate-report.md` (16-row before/after table; survived
mutant C; also refutes the claim's other half — plan mode *did* stop it — and
records a probe error that printed REFUSED from the wrong layer: "a refusal
from the wrong layer is not evidence about the layer under test").

**Status today: fixed.** `permission_modes.py:746` (`non_degrading` keyword);
`server.py:2499` (`_SELFMOD_SOURCE_WRITING_ACTIONS = frozenset({"deploy",
"rollback"})`), enforced at `server.py:2508`.

---

## Incident 7 — advertise-vs-dispatch drift, and its deeper shape ("dead vocabulary")

**Symptom (shallow).** Autopilot allowlists rendered verbatim into model
transcripts advertised tools with no dispatch branch: **23 of 86** workspace
and **5 of 45** observe tools were undispatchable, so runs spent steps on
`ERROR: unknown tool`. (The originally filed 18/42 measured a sub-surface
literal, not the surface shown to the model — Incident 12's pattern.)

**Symptom (deeper).** Some tools genuinely dispatch and are still dead: a
surface advertises, a policy admits, and a *second gate* refuses one step
later — invisible to any advertised-vs-dispatchable check. Worst instance: all
three tools the hosted claim reviewer's prompt named (`text_search`,
`file_read_range`, `file_find`) were denied on every hosted run by the cloud
privacy gate — 100% of its stated vocabulary dead. A refused-then-accepting
reviewer returned a bare claim with full confidence: nothing distinguished
"ran and found nothing" from "never allowed to run".

**Fix discipline (load-bearing).** Remove from the advertisement rather than
add a dispatch branch: all 23 undispatchable tools take a `root` resolved by
`harness_tools._resolve_root`, which at that time did **no** allowed-roots
check — adding dispatch would have handed autonomous runs unconfined
filesystem access. For the dead-vocabulary layer: derive the advertised set
from the gate that admits it (never restate a tool set beside its gate); a
reviewer whose only inputs were policy refusals returns `EVIDENCE_REQUIRED`,
never a verdict. Exempting the reviewer from the privacy gate was rejected: it
would trade a silent verification failure for an exfiltration channel.

**Evidence.** `drift-family-report.md` (measurements, mutation table,
alias-key laundering route); `dead-vocab-report.md` §1. Guard:
`tests/test_advertised_surface_drift.py`, which recomputes both sides from
source on every run.

**Status today: fixed (merged).** `tests/test_advertised_surface_drift.py`
exists at HEAD including the alias-key test (line 247) — see Incident 12 for
why that test's merge history matters.

---

## Incident 8 — floors reported as totals

Three instances of one shape: a floor (or ceiling) presented as a total.

**8a. `secret_scan` counts before it reads — open.** `harness_tools.py:1041`
increments `scanned` *before* the read; `except OSError: continue` silently
skips unreadable files, so an all-unreadable run returns `{"ok": True,
"findings": [], "files_scanned": N}` — indistinguishable from a clean scan, on
the very tool Incident 4 measured leaking a secret. **Status today: open**
(re-read at `99162cf9`; the proposed `unreadable` counter was never added).

**8b. `reclaim_orphans` counts failures as successes — open.**
`scripts/nightly_selfmod.py:514-519`: `selfmod.cancel(rid)` wrapped in
`except Exception: pass`, then `reclaimed += 1` unconditionally — a ceiling
reported as a total. **Status today: open** (re-read at `99162cf9`).

**8c. The regression selector's numbers were fiction — fixed.** Its default
mode read `git rev-parse HEAD` into a variable used only for truthiness, then
diffed `git diff HEAD` — the working tree against itself — so **committed work
was 100% invisible**. And selection was raw text matching: 69 of 81 selected
files on one lane came from the English word "check" in test prose. Fixed by
AST-based matching (blowup case 89→21 while precise symbols barely moved) and
exit 2 on vacuous selections. A known-answer test (planted mutation + two
full-suite runs) caught the first version at 4/6 recall. **A selector cannot
be validated by the count it reports about itself.** **Status today: fixed**
— the file at HEAD is the AST version: identifiers come from module-level
symbols via `ast.parse` (`module_api_symbols`), the default base is
`@{upstream}...HEAD` plus the working tree, and the flags are exactly
`--repo`/`--since`/`--format`/`--show-uncovered` with exits **0** and **2**
only. The over-broad guard some reports describe (`--max-fraction`, exit 3)
and the explicit base-resolution line on stderr never landed — do not cite
them; stderr carries only the selection summary and uncovered-identifier
lines.

**Evidence.** `sweep-of-the-fleet.md` findings 6-7;
`ratchet-selector-report.md` Item 2; `ratchet-doubles-report.md` NEW findings.

---

## Incident 9 — guards green on arrival

**Symptom / root causes, four flavors** — tests and guards that pass while
the thing they guard is broken, or that *assert the vulnerability itself*:

1. **The hole written down as intended behaviour.**
   `test_read_only_dispatch_reaches_test_discover` asserted verbatim that a
   read-only dispatch with no project bound reaches a developer-workflow tool —
   Incident 4's vulnerability encoded as a requirement, and the only test the
   fix broke. (`fix-critical.md` §4.)
2. **Bounds that provably never engage.** A recall-canary test feeding ~480
   chars against a 4000-char cap; negative assertions surviving deletion of
   the feature they check. Four catalogued in `sweep-of-the-fleet.md`
   ("Guards that cannot fail").
3. **Doubles that cannot absorb drift.** AST census: **1,231** monkeypatched
   doubles lack `**kwargs`. When the real signature gains a keyword, the
   double raises `TypeError` — into `except Exception: pass` (Incident 10) —
   and reads as "nothing was written", exactly what **11 negative assertions**
   (`assert written == []`) expected. Measured with drifted sinks: 8 failed, 5
   passed — the 5 that passed were the negatives. (`ratchet-doubles-report.md`.)
4. **Coverage that varies the wrong axis.** `_agent_verification_covers` read
   only `args["root"]`, never `args["path"]` — while 1,500 lines earlier the
   same file proved `path` is appended straight to the child argv. Its tests
   varied only `root`; a trivial `build_run(command="git --version")`
   satisfied "grounded validation passed". (`sweep-of-the-fleet.md` 1/1b.)

**The standard set against this** is fix-discipline point 4 below;
commit `278839e` mutation-proved a green-on-arrival assertion and is the
cited bar.

**Status today: partially fixed.** Flavor 1's test was rewritten (root bound +
negative half). Flavor 4 is fixed at HEAD — `server.py:18530` now narrows by
`path` and decides the no-mutation case explicitly (`all([])` is True was part
of the hole); its docstring carries the archaeology. Flavor 3 is **open as
repo-wide convention** — the specific sinks were widened; ~1,200 narrow
doubles remain. Flavor 2's guards were recorded, not re-verified here.

---

## Incident 10 — silent swallowing in counting loops

**Symptom.** `server._drain_deferred_distillations` returned
`stored: 0, deferred: 0` for a batch in which **every item raised** —
byte-identical to a legitimately empty batch. The campaign line printed
"lessons stored 0" with nothing saying the recorder never ran.

**Root cause.** A bare `except Exception: continue` inside a counting loop —
plus a third silent bucket nothing named (a recorder returning while claiming
neither a lesson nor a deferral).

**Fix shape.** `failed` counts raises (still absorbed — bookkeeping must not
break the run it services — but no longer silently); `skipped` counts unknown
signals and claim-nothing returns; buckets sum to the batch, pinned by a test
driving all five endings; `_EMPTY_DRAIN` gives early returns the full shape so
a `.get` default can never read as a measured zero. The `skipped` mutation
*survived* the first round — the branch existed untested.

**Evidence.** `ratchet-doubles-report.md` Item 2 (AST sweep: 6 sites — this
one fixed, 8a/8b reported-not-fixed, three cleared). Deliberately left open:
`server._feed_grounded_outcome`'s `except Exception: pass` still absorbs real
attribution failures, per the "bookkeeping must never break the run" contract.

**Status today: fixed.** `server.py:3460` returns `_EMPTY_DRAIN.copy()` on
early exit (`_EMPTY_DRAIN` defined at 3535).

---

## Incident 11 — fail-closed guard bypassed beside its own fix

**Symptom.** `permission_modes.risk_of` gained
`except CatalogUnavailable: return "dangerous"` — a fail-closed guard — while
*retaining* a broad `except Exception` two lines below that fell through to
`"ask"`. A classifier blinded by anything else (e.g. `ImportError` during
partial init — the scenario `CatalogUnavailable`'s own docstring cites)
reclassified `git_merge`, `sqlite_mutate`, `task_delete` as `ask` → `allow`
with an operator present: the guarded defect, reintroduced beside its fix.

**Second layer.** Even where the guard held, it was a no-op at all five
non-interactive call sites: `dangerous` maps to ASK in non-plan modes, and
ASK + `interactive=False` degrades to ALLOW — measured counterfactually, the
guard changed nothing on the production paths.

**Fix shape.** The blind branch returns `UNCLASSIFIED` (not degraded) and
catches `Exception` broadly *on purpose* — "every way of going blind must
grade the same" — with the reasoning written into the code as a 25-line
comment.

**Evidence.** `sweep-of-the-fleet.md` findings 2 and 9 (executed before/after:
`other_exception → allow, guard defeated`).

**Status today: fixed.** `permission_modes.py:596-623`.

---

## Incident 12 — cross-branch half-fixes and revision-drifted figures

**Symptom.** During the 20-branch fleet period (2026-08-11), the same defect
was fixed on one lane and carried — *plus a now-false docstring* — on three
others, including the integration HEAD. The alias-key laundering fix
(`278839e`, 23 lines) existed only on `work/13-drift-family`; `work/16`,
`work/20`, and HEAD carried the pre-fix guard whose docstring claimed the
laundering was impossible. A ghost alias injected on HEAD passed 21/21 guard
tests; the same injection against `work/13` failed, naming the ghost.
`git merge-tree` reported a conflict on the file, so a "take theirs" manual
resolution would have silently dropped the fix. Same period: the ratchet was
RED on some lineages and green on others, and the regression selector did not
exist on the fleet base lineage at all — "any lane on the base lineage that
reported running the selector could not have been running it".

**Discipline that caught all of this.** `git merge-base --is-ancestor <fix>
HEAD` before claiming any fix is on your lineage; cherry-pick with `-x` rather
than re-author when a sibling lane already fixed the site (`1ab0038b` is such
a cherry-pick); re-resolve every file:line anchor before repeating it.

**Evidence.** `sweep-of-the-fleet.md` (Lineage + finding 3); the Lineage
sections of both ratchet reports; `fix-critical.md` (opening, which corrects
the prior report's drifted anchors).

**Status today: fixed (merged).** The alias-key test is in the tree
(`tests/test_advertised_surface_drift.py:247`); the ratchet exits 0; the
selector exists at HEAD in its fixed form. The *discipline* is standing
practice.

---

## Incident 13 — the selfmod guard series (the pathology recurring)

Two more instances of Incident 1's pathology inside selfmod, both 2026-08-08 —
three days *before* the smoke gate was found.

**13a. The duplicate-objective guard that guarded nothing.** The loop
committed the same change twice with the guard active. Stored objectives carry
the model's rationale appended ("OBJ (WHY...)"); fresh proposals are compared
before the suffix exists, and `SequenceMatcher` divides by total length, so an
exactly restated objective scored ~0.59 against its own stored form and every
duplicate passed. Fix: compare the normalised objective head, plus token
overlap at 0.60 for rewords. Deliberately NOT deduplicated: different
objectives sharing an identifier. Evidence: commit `f7847dfa`.

**13b. `review()` was unsatisfiable and its verdict discarded.** Three bugs:
the branch-commit loop discarded `review()`'s return and committed
REVIEW-REJECTED code while logging "COMMITTED"; `reproducer_before` sat
outside the `require_kinds` filter, so the nightly loop (additions, not bug
fixes) could never satisfy it; `_test_inventory` scanned the venv (2,349 of
2,573 "test files"), so "test inventory was weakened" fired on **every**
git-mode review — 0 of 78 runs ever reached `reviewing`. A gate that always
fails is routed around; one that cannot fail is decoration. Evidence: commit
`7f453c2b`.

**Status today: fixed** (both commits are ancestors of `99162cf9`; the gate
stack of Incidents 1 and 6 was built on top of these fixes).

---

## The fix discipline (distilled from every report above)

When you fix anything in this repo, the reports converge on this sequence:

1. **Reproduce before fixing**, through the *production* path (real gate, real
   dispatcher), never a hand-written equivalent.
2. **RED at the final item count, failing behaviourally** — the failures must
   be the defect itself, not import errors; say which items fail on signature
   vs. behaviour.
3. **Keep a control case.** "A guard that refuses everything is not a guard"
   (`fix-critical.md`): every new refusal ships beside a test that the
   legitimate path still works.
4. **Mutation-prove every new guard.** Plant the violation, watch the named
   test fail, revert (byte-exact backup, `sha256sum -c`), watch it pass. Hold
   plants *outside* the scanned tree so a scanner cannot scan its own plant.
5. **Corroborate any count of exactly 0 or 1 three ways** — a vacuity floor,
   an independent second extractor, a probe entry that must be seen. "A count
   of 0 on five surfaces is the classic early-abort tell". Unpipe exit codes —
   one report shipped `tail`'s exit code as the checker's.
6. **Re-resolve file:line anchors and re-verify lineage** (`git merge-base
   --is-ancestor`) before repeating any claim from an older report — or this one.

---

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). All commit hashes confirmed
via `git log`; every "Status today" re-checked by reading the current tree;
the ratchet exit code obtained by running the checker, unpiped.

Re-verification one-liners (run from the repo root):

- Incident commits still in history:
  `for h in 7bdfa19f c1c1475e cee18669 1ab0038b ae9503b0 c60ba932 f7847dfa 7f453c2b 2cec3272; do git show $h --stat | head -3; done`
- Smoke gate still real: `grep -n "def record_smoke" selfmod.py` (expect ~915)
  and `grep -n "deliberately NOT" server.py` (comment near 2226).
- Ratchet state: `python scripts/check_error_signals.py; echo $?` (0 = green;
  1 means a new literal `ERROR:` site — migrate it, never regenerate).
- Incidents 8a/8b still open? `sed -n '1030,1056p' harness_tools.py` (open
  while `scanned += 1` precedes the read, no unreadable counter) and
  `sed -n '505,522p' scripts/nightly_selfmod.py` (open while `reclaimed += 1`
  follows `except Exception: pass`).
- Incident 6 gate wired: `grep -n "_SELFMOD_SOURCE_WRITING_ACTIONS" server.py; grep -n "non_degrading" permission_modes.py`
- Incident 11 fix present: `sed -n '596,623p' permission_modes.py` (expect the
  UNCLASSIFIED return and the width-is-the-point comment).
- Incident 12 fix merged: `grep -n "alias_keys_and_targets" tests/test_advertised_surface_drift.py`
- Primary sources readable: `ls .superpowers/sdd/work/` (21 reports at this
  writing).
