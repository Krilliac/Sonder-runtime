# Dead vocabulary: admit-then-deny (#44, #45)

Worktree `D:\sonder-wt\16-dead-vocab`, branch `work/16-dead-vocab`, parent
`dad98ac` (the drift lane's work), which sits on
`feat/verified-fetch-modes-calibration` @ `9f377f1`. Nothing here is compared
against `main`.

`.superpowers/sdd/work/rereview-drift.md` does **not** exist in this worktree;
it lives only in `D:\sonder-wt\13-drift-family`. It was read there.

## 0. The shape

A surface advertises a tool, a policy admits it, and a **second gate** refuses
it one step later. The model is told it can call the tool, spends a step
calling it, and is refused. An advertised-vs-dispatchable check cannot see
this, because the tool genuinely dispatches — the refusal happens on either
side of the dispatch table, not in it.

Root cause common to every instance: `_agent_tool_help` filtered on
`read_only` / `cloud` / `unsafe`, while `_agent_impl` gates three more times,
name-unconditionally, on `project_scope` / `allow_web` / `allow_location`.
Two of those three live *inside* `_agent_dispatch`'s own branch bodies, so
`dispatch_names()` counts them as dispatchable by construction.

---

## 1. #44 — the verifier whose whole vocabulary was dead

### 1.1 Measurement

`_AGENT_CLAIM_REVIEW_TOOLS` has five members. Three are in
`_CLOUD_AGENT_LOCAL_ONLY_TOOLS` and refused by
`_cloud_agent_tool_policy_error` on every hosted run:

| Tool | Hosted | Named in the reviewer prompt |
|---|---|---|
| `text_search` | **denied** | yes |
| `file_read_range` | **denied** | yes |
| `file_find` | **denied** | yes |
| `repository_symbol_index` | admitted | **no** |
| `project_detect` | admitted | **no** |

**3 of 3** tools the reviewer prompt names are dead on a hosted run — 100 % of
its stated vocabulary. The two that survive are the two it never mentions, and
they were unnamed on **local** runs too, so the mechanism advertised 3 of its 5
capabilities everywhere and 0 of its usable ones when hosted.

The refusal is not incidental. It reads: *"local-only tool 'text_search' is
disabled inside a hosted agent so private workspace or machine data cannot
enter the hosted model transcript."*

### 1.2 Reproduction (before / after)

Driving `_agent_impl` with the reviewer proposing `text_search` — which is both
what its prompt names and what the deterministic `_agent_exact_negative_action`
emits before any model is consulted — and spying on
`_agent_dispatch_observed`:

```
BEFORE
  cloud=False  tools dispatched: ['file_read', 'text_search', 'text_search']
               text_search reached dispatch: True
  cloud=True   tools dispatched: []
               text_search reached dispatch: False
```

The identical run verifies twice locally and zero times hosted.

```
AFTER
  test_hosted_claim_review_tool_is_refused_before_dispatch  PASSES
    (asserts the local run still dispatches text_search, so the test is not
     measuring an empty set)
  the hosted reviewer prompt now names: project_detect, repository_symbol_index
  the local reviewer prompt now names all five
```

### 1.3 Does the reviewer return a confident verdict it cannot support?

**Yes — reproduced.** The dangerous path is `accept`, not `continue`. A
reviewer that asks for `text_search`, receives a policy refusal, and then
accepts — the natural response to a refusal it cannot act on — caused
`_agent_impl` to return the bare claim:

```
  tools dispatched          : []
  verification actually ran : False
  returned output           : 'The Persistent autopilot heading was not found.'
  is EVIDENCE_REQUIRED      : False
```

Zero verification dispatches, no marker, full confidence. Nothing in the code
distinguished "the tool ran and found nothing" from "the tool was never allowed
to run". `_agent_tool_observation_ok` correctly returns `False` for the refusal
text, but that only withholds *evidence credit* — it did not stop the accept.

### 1.4 Fix chosen: (a) + (c). (b) rejected.

* **(a) name the surviving tools.** Done, but not by hardcoding a new trio —
  `_agent_claim_review_tools(cloud)` derives the admitted set from
  `_cloud_agent_tool_policy_error`, and both the system prompt and the JSON
  schema render from it. This is the same anti-drift shape `_agent_tool_help`
  already uses; restating a tool set beside its gate is exactly what produced
  the defect. The schema validation now checks the same admitted set instead of
  the superset, so a hosted reviewer naming `text_search` is corrected
  immediately rather than a step later.
* **(c) report inability rather than proceeding.** Required, and required
  independently of (a): even with a correct vocabulary a refusal can occur, and
  the reviewer must not convert it into a verdict. `_agent_impl` now tracks
  `claim_review_policy_refused` / `claim_review_verified`; an `accept` that
  follows only policy refusals, with no claim-review tool ever producing
  evidence, returns `EVIDENCE_REQUIRED`. Once any claim-review tool has
  actually produced evidence the release valve opens and a later accept stands.
* **(b) stop denying these three on the reviewer path — rejected on the
  merits.** The denial is a privacy boundary, not an oversight: its own message
  says it exists so private workspace content cannot enter a hosted model
  transcript. The reviewer path is the *worst* place to exempt, because
  `text_search` / `file_read_range` / `file_find` return file contents straight
  into that transcript. Taking (b) would trade a silent verification failure
  for a silent data-exfiltration channel.

Two further things fell out and were fixed:

* `_agent_exact_negative_action` hardcoded `text_search` and runs **before** the
  reviewer model, so the *host itself* proposed a tool the host would refuse.
  It now yields to the model reviewer when `text_search` is not admitted.
* An empty tool on `continue` is now accepted as the honest "I cannot settle
  this" signal. It is the shape this function's own parse-failure fallback
  already returned, `_agent_impl` already handles it, and it is strictly safer
  than an unsupported `accept`.

---

## 2. #45 — re-measured F1 / F2 / F4

Method: branch-level, alias-canonicalised through `_canonical_agent_tool_name`
(canonicalisation happens **before** the gates in `_agent_impl`, so the gates
see canonical names). Dispatch branches were extracted by AST as alias *groups*,
not names, and the branch extractor was cross-checked against
`tool_capabilities.dispatch_names` — symmetric difference empty — and floored
(≥ 90 branches) so an empty extractor fails loudly. Live registration: 184
tools, 109 dispatch branches, 117 dispatch names.

| | Filed | Re-measured (names) | Re-measured (branches) | Agrees? |
|---|---|---|---|---|
| **F1** `_AUTOPILOT_WORKSPACE_TOOLS − _PROJECT_BOUND_AGENT_TOOLS` | 5 | **5** | **5** | yes |
| **F2** `AGENT_TOOL_HELP` dead at the same gate | 21 | **21** | **21** | yes |
| **F4** `_orchestrator_agent_worker` web tools | 3 | **3** | **3** | yes |

All three filed figures reproduce exactly. Names equal branches in every case —
no alias inflation here, because none of these tools has an alias.

**F1 (5):** `artifact_generate`, `game_generate_and_test`,
`game_reference_suite`, `run_code`, `run_project`. Rendered verbatim into the
transcript as `HOST TOOL ALLOWLIST (cannot be expanded by the model)`.

**F2 (21):** `apply_learned`, `artifact_generate`, `game_generate_and_test`,
`game_generation_campaign`, `game_reference_suite`, `learn_preference`,
`master_cancel`, `master_orchestrate`, `master_retry`,
`memory_embedding_backfill`, `memory_interaction_embedding_backfill`,
`memory_privacy_repair`, `memory_quality_repair`, `offload`, `run_code`,
`run_project`, `self_heal_repair`, `set_context_size`, `tune_emotion_vectors`,
`update_emotion_vectors`, `workflow_run`.

**F4 (3):** `web_search`, `web_fetch`, `weather_lookup`. Worth stating why this
one is the purest instance of the shape: these three pass the read-only gate
(`REPOSITORY_READ_ONLY_TOOLS` lists all three) **and** the project-bound gate
(`_PROJECT_BOUND_AGENT_TOOLS` lists all three). Their only refusal is
`allow_web` *inside the dispatcher*. Nothing upstream of dispatch could have
seen it.

### Additions to the filed picture (not disagreements)

* `REPOSITORY_AGENT_TOOL_HELP` is clean at **0** against the project-bound
  gate. The read-only surface was never affected; F2 is a full-help-surface
  defect only.
* `approximate_location_lookup` is a **fourth** `allow_web`-gated tool (and the
  only `allow_location`-gated one). It is not part of F4 because it is not on
  the repository help surface the orchestrator worker sees — but it *is* dead
  on the full help surface whenever `allow_web` or `allow_location` is off, and
  the fix covers it.

---

## 3. The extended guard, and proof that it binds

`tests/test_advertised_surface_drift.py` gains an admit-then-deny section. The
core assertion is `test_agent_help_advertises_nothing_a_run_gate_will_refuse`:
for **every combination** of the run flags, no name the help advertises may be
one that `_agent_run_tool_policy_error` refuses on that same run.

The production fix and the guard read the *same* predicate
(`_agent_run_tool_policy_error`), so they cannot drift from each other. The two
declared constants `_AGENT_WEB_GATED_TOOLS` / `_AGENT_LOCATION_GATED_TOOLS` are
asserted equal to the `if not allow_web:` / `if not allow_location:` guards
AST-extracted from `_agent_dispatch`'s branch bodies, so the constants cannot
drift from the code they describe.

### Mutation proof — each planted, run, reverted

| Mutation | Result |
|---|---|
| drop `allow_web` from the `_agent_tool_help` filter | **2 failed** — `test_agent_help_advertises_nothing_a_run_gate_will_refuse`, `test_orchestrator_worker_help_names_no_tool_its_own_flags_refuse` |
| drop `project_bound` from the filter | **2 failed** — `test_agent_help_advertises_nothing_a_run_gate_will_refuse`, `test_project_bound_help_still_advertises_a_usable_surface` |
| stop narrowing the autopilot allowlist on a project-bound run (F1) | **1 failed** — `test_autopilot_workspace_allowlist_survives_the_project_bound_gate` |
| drift `_AGENT_WEB_GATED_TOOLS` from the dispatcher | **2 failed** — `test_flag_gate_extractors_cannot_go_vacuous`, `test_orchestrator_worker_help_names_no_tool_its_own_flags_refuse` |

All reverted; `15 passed` after. The guard binds in every direction tested.

Non-vacuity is asserted rather than assumed: the flag-gate extractor must see
`web_search`/`web_fetch`, the project-bound gate must refuse ≥ 10 dispatchable
tools, and `test_project_bound_help_still_advertises_a_usable_surface` asserts
the new filter **narrows** the surface without emptying it (`bound < unbound`,
`len(bound) >= 30`) — a filter that removed everything would otherwise satisfy
every "advertises nothing refused" assertion trivially.

### One test defect found and corrected in my own work

My first version of the #44 prompt extractor scanned
`inspect.getsource(_agent_negative_claim_review)` for tool names. Once the
vocabulary became dynamic that measured the wrong thing entirely — it started
matching the *comments I had just written* about `text_search`. Replaced with a
reader that captures the actually-rendered system prompt and review prompt via
`_build_system` / `_make_generate`, which is what the model sees, plus a
non-vacuity check that it sees ≥ 4 names locally and ≥ 2 hosted.

---

## 4. The test-selection rule

The old rule was **prose**, not code: "every test file referencing
`_AUTOPILOT_*`, `tool_manifest`, `AGENT_TOOL_HELP`, `_loop_dispatch` or
`workflow`". That is a list of terms someone thought of while writing the
change, so it can only ever cover surfaces already in mind.

Verified defect: `tests/test_read_only_agent_policy.py` contains **zero** of
those five terms (`grep -c` → 0), so the rule provably could never select it —
for a change that moved three tools across exactly the gate that file tests. It
references `_agent_dispatch` (13×), `REPOSITORY_READ_ONLY_TOOLS`,
`_PROJECT_BOUND_AGENT_TOOLS` and `_agent_tool_help`.

Fix: `scripts/select_regression_tests.py`. It derives its terms from the diff —
module-level symbols of changed source files that changed lines either
reference or sit inside — and selects any test naming one. It selects on the
surface's *own name*, so it cannot miss a surface the change hit.

Two design points, both learned by getting it wrong first:

* A raw word scan of changed lines pulls English out of comments and
  docstrings ("Advertise", "Deriving", "restating") and selected **304 of 313**
  files — a "selection" that selects everything. Restricting to module-level
  AST symbols of the changed file fixes it: a test cannot reference a local
  variable, so local names must not select.
* Keying on the bare **module name** selects every test that imports `server` —
  129 of 313. Module name is now a *fallback*, used only when no API identifier
  changed, and the output says so when it fires.

On this change: **18 changed identifiers → 63 of 313 files**, including
`tests/test_read_only_agent_policy.py`, matched on four separate symbols.

The script exits **2** when the selection goes vacuous (no identifiers, or zero
tests matched) with an explicit message that this is an infrastructure failure
and must not be read as "nothing to run" — the 0-or-1 tell, made loud.

Known characteristic, deliberately not "fixed": a changed *script* contributes
generic entry-point names (`main`, `parse_diff`, `run_git`), which over-select.
Over-selection is the safe direction and adding those to the stopword list
could under-select a test that legitimately names them. Left as is, and
reported.

---

## 5. Test evidence (verbatim pytest summary lines)

**#44** — `tests/test_claim_review_hosted_vocabulary.py` (10 items), RED
captured at the final item count against pre-fix `server.py` (restored with
`git show HEAD:server.py`; **`git stash` was never run**):

```
7 failed, 3 passed in 1.41s
```

GREEN, same file, after the fix:

```
10 passed in 1.15s
```

**#45** — `tests/test_advertised_surface_drift.py` (15 items), RED at the final
item count against `server.py` at `8ad7306`:

```
5 failed, 10 passed in 1.58s
```

GREEN, same file:

```
15 passed in 1.30s
```

**Regression set**, chosen by the new rule (`--since 8ad7306`), 63 files:

```
1789 passed, 3 skipped, 1 warning in 86.92s (0:01:26)
```

Re-run after the selection script itself became tracked, which widened the set
to 106 files (its own generic symbol names now select too) — a strict superset
of the 63:

```
2808 passed, 14 skipped, 1 warning, 4 subtests passed in 219.88s (0:03:39)
```

Both runs completed every selected file; neither number is a floor from an
early abort. The 1789 → 2808 jump is entirely explained by 63 → 106 files, and
was checked before being reported rather than after.

Focused confirmation of the four files that matter most, including the one the
old rule could never select:

```
tests/test_advertised_surface_drift.py tests/test_claim_review_hosted_vocabulary.py
tests/test_read_only_agent_policy.py tests/test_agent_help_dispatch_drift.py
56 passed in 2.40s
```

The full suite (~522 s) was **not** run.

---

## 6. New findings

**IMPORTANT (new, fixed here).** *A verifier that could not verify returned a
confident verdict.* Independent of the dead vocabulary that triggered it,
`_agent_impl` had no way to distinguish "the evidence tool ran and found
nothing" from "the evidence tool was never permitted to run". A claim-review
`accept` following only policy refusals returned the unverified negative claim
with full confidence and no marker. This is the failure mode with the worst
blast radius in the family, because the thing that failed silently is the thing
whose job is catching silent failures. Fixed; guarded by
`test_accept_after_a_policy_refusal_does_not_return_a_bare_claim`, with
`test_accept_after_successful_verification_is_still_accepted` and
`test_local_accept_without_any_refusal_is_unchanged` proving the fix is scoped.

**IMPORTANT (new, fixed here).** *The host proposed a tool the host would
refuse.* `_agent_exact_negative_action` is deterministic, hardcodes
`text_search`, and runs **before** the reviewer model is consulted. On a hosted
run it therefore guaranteed a refusal without any model involvement — so the
defect was not merely "the model was told the wrong thing", it was reachable
with no model in the loop at all.

**MINOR (new, reported, not fixed).** `_PROJECT_BOUND_AGENT_TOOLS` (111 names)
contains **24** names with no `_agent_dispatch` branch: the same 23 the drift
lane removed from `_AUTOPILOT_WORKSPACE_TOOLS` in `277fd27`, plus
`fetch_artifact`. This is inert rather than harmful — it is a permit set, not
an advertising surface, so an unreachable name in it grants nothing and
promises nothing. Deliberately **not** asserted as a subset of the dispatch
branches in the new guard, with a comment saying why, so a future reader does
not "fix" it by adding dispatch branches that would hand a project-bound run
the unconfined filesystem access `b8a15ef` removed.

**Not a defect, recorded to close the question.** The filed F1/F2/F4 figures
are the first in this family to survive re-measurement unchanged. The prior
lane's warning that "any remaining figure in this defect family should be
re-derived before use" was still worth honouring — but it did not bite here.

---

## 7. Commits

- `8ad7306` — Stop the negative-claim reviewer naming a vocabulary hosted
  policy denies (#44)
- `3e27ae6` — Stop advertising tools that `project_scope`/`allow_web` refuse a
  step later (#45 F1/F2/F4, the extended guard, and
  `scripts/select_regression_tests.py`)
- `2555cae` — Record the project-bound gate in the autopilot allowlist
  invariant comment
- this report committed separately

Checkout left clean on `work/16-dead-vocab`. Nothing pushed. **`git stash` was
never run; `refs/stash` untouched.** No `git add -A`; staging was always by
explicit path. No sibling worktree was modified, no vendored
`app/build/**/local-system/*.py` was touched, no network call was made, and the
operator's memory DB was not touched.
