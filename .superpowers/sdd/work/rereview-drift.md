# Re-review: advertise-vs-dispatch drift family (`277fd27`, `dad98ac`)

Worktree `D:\sonder-wt\13-drift-family`, branch `work/13-drift-family`, parent `b8a15ef`.
Every number below was produced by running something in this checkout. Nothing is
restated from `drift-family-report.md`; where I quote a lane figure I say so and give my
own measurement beside it. The full suite was **not** run — see §6.

Pre-fix figures were measured by pinning `sys.path` at a scratch copy of
`git show b8a15ef:server.py` (no file in this worktree was modified to do it), so
`server.__file__` provably pointed at the pre-fix source for those runs.

---

## 1. Audit of the re-measurement method (question A)

### 1.1 A third, independent method for 184

The lane's two methods were the live FastMCP tool manager and an AST sweep for
`@mcp.tool()` on module-level `def`s. I added a third that shares no machinery with
either: a **pure text scan of every `*.py` in the repo root**, matching a decorator line
`@(mcp|self.mcp|_mcp|server.mcp).tool` followed within 8 lines by a `def`, at any nesting
depth and in any file — no `ast`, no import, no FastMCP internals.

| Method | Count |
|---|---|
| M1 live `server.mcp._tool_manager._tools` | **184** |
| M2 AST over module-level `@mcp.tool` defs | **184** |
| M3 text scan of all repo-root `*.py` | **184** |

`M1 △ M3 = ∅`, `M2 △ M3 = ∅`.

**Do M1 and M2 share an assumption?** No, and this is checkable rather than assertable.
M1 reads whatever is registered at runtime by any route; M2/M3 read only decorator syntax
in source. `reloadable_mcp.py:237` (`manager.add_tool(fn, *args, **kwargs)`) is a
programmatic registration path that M2 and M3 are structurally blind to and M1 is not — so
if it registered anything extra at import, M1 would exceed M2. It does not. Equally, M2 is
restricted to `module.body`; I checked for nested or conditionally-defined `@mcp.tool`
functions (an M2-only blind spot) and found **none**. The agreement is therefore
informative, not circular.

One loose end resolved: `server.py` contains **185** occurrences of the string
`@mcp.tool` but 184 decorators. The 185th is `server.py:13095`, a comment added by
`277fd27` itself (`# advertising surface that no @mcp.tool() backs.`). Accounted for.

**Verdict: the 184 figure survives audit.**

### 1.2 `_agent_dispatch` has no fallthrough

Verified three ways, the last by execution:

* `server.py:14197` is `def _agent_dispatch(`; its source body ends with a `Return` node
  whose unparsed value contains `unknown tool` (last line: `return "ERROR: unknown tool
  '%s'." % tool_name`), and the function contains 117 `return` statements in total;
* the body's final statement *is* that `Return`, so control cannot leave the function
  without hitting it;
* **executed**: `_agent_dispatch("__no_such_tool__", {}, read_only=False)` returns
  `ERROR: unknown tool '__no_such_tool__'.` and `_agent_dispatch("test_run", {},
  read_only=False)` returns `ERROR: unknown tool 'test_run'.`

So a name with no branch is genuinely unreachable. **Confirmed.**

Note for §5: with `read_only=True` the same calls return the *repository read-only*
refusal instead, i.e. the read-only gate fires before the unknown-tool return.

### 1.3 The per-branch grouping (the load-bearing correction)

Reproduced independently, and the arithmetic closes exactly:

| Quantity | My measurement (pre-fix, at `b8a15ef`) | Lane |
|---|---|---|
| `_loop_dispatch` branches | **68** | 68 |
| distinct names incl. aliases | **85** | 85 |
| names in the `"Valid action types:"` reply | **58** | 58 |
| names in the docstring example block | **35** | 35 |
| per-**name** gap (the filed 27) | **27** | 27 |
| per-**branch** gap (hidden capability) | **10** | 10 |
| advertised-but-unimplemented | **0** | 0 |

The 10 hidden branches I extracted are byte-identical to the lane's list: `work`
(=`workbench_agent`), `directory_create`, `file_read_range`, `artifact_risk_inspect`,
`process_list`, `process_memory_risk_inspect`, `data_inspect`, `checklist_create`,
`checklist_update`, `checklist_show`. `27 = 10 hidden branches + 17 alias names`, and I
enumerated all 14 multi-name branches carrying those 17 aliases
(`('artifact','artifact_generate','assetgen')`, `('work','agent','workbench_agent')`, …).

The grouping is correct: `_loop_dispatch`'s alias branches are literally
`action_type in {...}` tuples, so one branch = one capability is the right unit, and the
guard's `if not advertised.intersection(branch)` is the correct per-branch predicate.
Post-fix I measure 68 advertised, 0 branches unnamed, 0 names unimplemented, 17 names
still unadvertised (the aliases). **Correction stands.**

### 1.4 Every other lane figure, re-derived

All measured by me, pre-fix where marked. All reproduce:

* `_AUTOPILOT_OBSERVE_TOOLS` 45 advertised / 40 dispatchable / **gap 5** (exact names match);
* `_AUTOPILOT_WORKSPACE_TOOLS` 86 / 63 / **gap 23** (exact names match);
* the filed `18 of 42` **does** reproduce on the extras literal alone — 42 names, 18
  undispatchable — confirming the lane's diagnosis that it measured a sub-surface, not
  that it was arithmetically wrong;
* `AGENT_TOOL_HELP` 130 / 107 / gap 23, and `_KNOWN_UNDISPATCHABLE_HELP_ENTRIES`
  (`tests/test_agent_help_dispatch_drift.py:45`) is **exactly** that 23-name set (set
  equality, not just cardinality);
* `tool_manifest()` 141 names, 3 unregistered pre-fix (`save`, `run`, `delete`), **0**
  post-fix;
* post-fix: observe 37 / workspace 63, both gap 0, observe ⊆ `REPOSITORY_READ_ONLY_TOOLS`.

**One figure I would correct.** The report states, twice and in the present tense
(§2 "*46 registered tools are absent from `tool_manifest()`*" and §5 MINOR "*It is also
the least covered: 46 registered tools appear nowhere in it*"), a number that is
pre-fix. I measure **46 at `b8a15ef`** and **43 at HEAD** — the lane's own manifest-key
fix added `workflow_save`/`workflow_run`/`workflow_delete` and closed three of them.
Cosmetic in impact, but it is precisely this project's recurring failure mode (a figure
measured at one revision and reported as a fact about another), so it should be fixed
before the number is quoted onward.

---

## 2. The `#22 = 0` result (question B)

### 2.1 The lane's stated defence is false as written

The report §1/#22 defends the five zeros with: "*the **same** extractor did surface the 3
real hits on `tool_manifest()` — so it is not blind*."

**It is not the same extractor.** The guard has three separate paths
(`tests/test_advertised_surface_drift.py`):

* `_help_advertised` (line 49) — parses `- name:` bullets, used for the two help surfaces;
* `_manifest_advertised` (line 62) — splits slash-separated dict keys, used for
  `tool_manifest()`;
* no parser at all for `REPOSITORY_READ_ONLY_TOOLS` / `_AUTOPILOT_OBSERVE_TOOLS` /
  `_AUTOPILOT_WORKSPACE_TOOLS` — those are `frozenset(getattr(server, label))`.

Measured cross-application: `_help_advertised(tool_manifest())` yields **0** names and
`_manifest_advertised(AGENT_TOOL_HELP)` yields **0** names. The two parsers are disjoint in
what they can see. Finding 3 with one says **nothing** about whether the other is blind.
The defence does not support the conclusion.

### 2.2 The conclusion nevertheless survives, by better evidence

Two of the five surfaces need no defence at all: `_AUTOPILOT_OBSERVE_TOOLS`,
`_AUTOPILOT_WORKSPACE_TOOLS` and `REPOSITORY_READ_ONLY_TOOLS` are Python `frozenset`
literals read directly — there is no parser to go blind, and `0` there is a set
difference, not an extraction. I confirm `unregistered = []` for all three (n = 37, 63, 55).

For the two that *do* have a parser, I confirmed 0 by an independent, deliberately
over-broad method: a greedy regex sweep for **every** `snake_case` token anywhere in the
help text, which cannot go vacuous by failing to match a bullet shape.

| Surface | lane parser | greedy sweep | non-registered tokens that are real loop/dispatch names | registered tools the lane parser **missed** |
|---|---|---|---|---|
| `AGENT_TOOL_HELP` | 130 names | 239 tokens | **0** | **0** |
| `REPOSITORY_AGENT_TOOL_HELP` | 55 names | 98 tokens | **0** | **0** |

The last column is the strongest evidence available and is stronger than what the lane
offered: the greedy sweep found **zero** registered tool names that the lane's bullet
parser failed to pick up, on either surface. The parser is not under-matching.

**Verdict on the `#22 = 0` defence: the reasoning is wrong, the result is right.** The
report should replace its defence paragraph with the above; a future reader relying on
"same extractor" would be relying on something untrue.

---

## 3. Does the guard bind? (question C)

`tests/test_advertised_surface_drift.py`, **10 collected**, `10 passed in 1.91s`.

Mutations were applied by injecting into the imported `server` module via a pytest plugin
rather than editing `server.py`. This is faithful for these mutations because every
assertion under test reads the module attribute at call time; the only source-derived
inputs in the file are `inspect.getsource(_loop_dispatch/_agent_dispatch/server)` and
`loop.__doc__`, none of which any mutation touches. It also left the worktree clean while
a sibling lane was active. The three lane mutations reproduce **exactly**:

| # | Mutation | Result | Lane claimed |
|---|---|---|---|
| 1 | `"__ghost_tool__"` → `_AUTOPILOT_OBSERVE_TOOLS` | **3 failed** — `test_no_surface_advertises_an_unregistered_tool`, `test_autopilot_allowlists_only_name_dispatchable_tools`, `test_autopilot_observe_allowlist_survives_repository_read_only_policy` | 3, same IDs ✓ |
| 2 | `"test_run"` → `_AUTOPILOT_WORKSPACE_TOOLS` | **1 failed** — `test_autopilot_allowlists_only_name_dispatchable_tools` | 1, same ID ✓ |
| 3 | drop `"checklist_show"` from `_LOOP_ACTION_TYPES` | **2 failed** — `test_loop_advertises_every_action_type_it_implements`, `test_loop_docstring_and_error_reply_advertise_the_same_vocabulary` | 2, same IDs ✓ |

I ran four more.

| # | Mutation (mine) | Result |
|---|---|---|
| 4 | `"test_run"` (registered, **un**dispatchable) → `REPOSITORY_READ_ONLY_TOOLS` | **new guard: 10 passed — MISS.** Caught only by pre-existing tests (below). |
| 5 | ghost laundered through `_AGENT_TOOL_ALIASES` onto `_AUTOPILOT_OBSERVE_TOOLS` | 2 failed — caught |
| 6 | ghost laundered through `_AGENT_TOOL_ALIASES` onto `AGENT_TOOL_HELP` | **new guard: 10 passed — MISS.** Caught by `test_agent_help_dispatch_drift.py`. |
| 7 | ghost laundered through `_AGENT_TOOL_ALIASES` onto **`tool_manifest()`** | **45 passed across 4 guard files — MISS, uncaught anywhere.** |
| 8 | the exact regression: put `process_list`/`process_memory_risk_inspect`/`task_progress` back in `_AUTOPILOT_OBSERVE_TOOLS` | 3 failed — the new subset assertion **plus both updated old tests** |
| 8b | `_AUTOPILOT_OBSERVE_TOOLS = frozenset()` (vacuity probe) | 2 failed, incl. `test_extractors_cannot_go_vacuous` — the subset assertion **cannot** be satisfied by emptying it |

### Mutation 4 — over-fitting to the surfaces the lane changed

`REPOSITORY_READ_ONLY_TOOLS` **is** one of the five surfaces `_advertising_surfaces()`
enumerates, but only `test_no_surface_advertises_an_unregistered_tool` consumes it — the
dispatchability subset assertion (`test_autopilot_allowlists_only_name_dispatchable_tools`)
iterates over the two autopilot labels only. So an undispatchable-but-registered name
re-added to the repository agent's own allowlist — which is *literally the defect
`b8a15ef` fixed, one commit earlier* — passes this file 10/10.

Severity is reduced by defence in depth: the mutation is caught by four pre-existing
tests, including `test_agent_help_dispatch_drift.py::
test_known_undispatchable_allowance_cannot_hide_a_read_only_regression`, which is
`b8a15ef`'s own guard. **And** I verified the missing assertion is currently satisfiable:
`REPOSITORY_READ_ONLY_TOOLS - dispatch_names(_agent_dispatch) = []` (55 names, gap 0), so
adding `label` to the loop at line 246 is a one-word change with no other consequence.
Ruling: **Minor**, worth closing.

### Mutation 7 — the alias allowance *can* launder a fake name (Important)

`test_agent_tool_aliases_all_resolve_to_registered_tools` (line 229) carries the docstring
*"The alias allowance above must not be able to launder a fake name"*, and the report §3
repeats it. Both are false. That test checks alias **targets** (`item[1] not in
registered`); the allowance at line 222 subtracts alias **keys**
(`names - registered - aliases`). Nothing checks that a key is real.

So: add `_AGENT_TOOL_ALIASES["__ghost_manifest__"] = "memory_search"` and advertise
`__ghost_manifest__` on `tool_manifest()`. The name is backed by no `@mcp.tool()` and has
no `_agent_dispatch` branch (I verified alias keys are *not* resolved inside
`_agent_dispatch` — `_AGENT_TOOL_ALIASES` appears nowhere in its source; the nine existing
alias keys are in `dispatch_names` because they have their own literal branches, and
resolution happens separately at `server.py:15187` via `_canonical_agent_tool_name`, used
on the allowlist at `server.py:16205`). Result: **45 passed** across
`test_advertised_surface_drift`, `test_agent_help_dispatch_drift`,
`test_tool_capabilities`, `test_mcp_primitives`. Nothing in the repo catches it.

This is the same defect class the lane just fixed (`#22`, unregistered names on
`tool_manifest()`), on the same surface, reachable through the guard's own allowance.
Fix: assert `set(server._AGENT_TOOL_ALIASES) <= dispatch_names(_agent_dispatch)` — all
nine current keys already satisfy it, so it is a free assertion.

---

## 4. The deliberate omissions (question D)

**17 loop aliases left unadvertised — RIGHT, and provably costless.** The concern would be
hidden capability; there is none. Post-fix I measure *zero* branches with no advertised
name, so every capability `_loop_dispatch` implements is reachable by a name the tool tells
you about. The aliases are pure spelling variants inside `action_type in {...}` tuples.
The lane's rationale (listing `assetgen` beside `artifact_generate` implies two
capabilities) is sound, and — more importantly — the guard asserts the *per-branch*
property, so a future branch added with only an alias name would still fail. The choice is
enforced, not merely intended. One nit: the guard permits the canonical name to be any
member of the branch, so `improvement_report` is advertised while the actual MCP tool is
`system_improvement_report`; harmless, but it means "canonical" is unpinned.

**`AGENT_TOOL_HELP` / `_KNOWN_UNDISPATCHABLE_HELP_ENTRIES` left alone — RIGHT; the stated
collision risk is real and I confirmed it.** `D:\sonder-wt\12-merge-dispatch` (branch
`work/12-merge-dispatch`) has **uncommitted** modifications to `server.py`
(`git diff HEAD --stat`: 340 changed lines) whose hunk headers are
`@@ -14538,6 +14538,21 @@ def _agent_dispatch` and
`@@ -15383,164 +15398,173 @@ def _agent_dispatch` — i.e. a live agent is rewriting the
body of `_agent_dispatch` right now, including a 164→173-line replacement. Editing
`AGENT_TOOL_HELP`'s 23 parked entries would have meant editing the dispatchability of the
very function being rewritten. Deferral justified. (Read-only inspection only; I did not
touch that worktree.) I separately confirmed the parked allowance is exact, not a
catch-all: `_KNOWN_UNDISPATCHABLE_HELP_ENTRIES` **equals** the measured 23-name help gap,
by set equality — it cannot absorb a 24th name.

**46 tools missing from `tool_manifest()` left alone — RIGHT as a scope call, wrong as a
number.** It is the opposite direction (hidden capability, not phantom capability) and out
of this defect family; leaving it is correct. But see §1.4: the live figure is **43**, not
46, and the report asserts 46 in the present tense in two places. Also note this omission
is what makes mutation 7 bite — `tool_manifest()` is the least-guarded advertising surface
in the codebase and is now the only one with a working laundering route.

---

## 5. The newly-fixed Important, and the sweep for its shape (question E)

**Did the two old tests really encode the defect as the spec? Yes — verbatim.**

`tests/test_process_risk_server.py::test_process_tool_registration_help_reload_and_autopilot`,
before `277fd27`:

```python
        assert name in server._AUTOPILOT_OBSERVE_TOOLS
        assert name not in server.REPOSITORY_READ_ONLY_TOOLS
        assert server._autopilot_tool_policy({"policy": "observe"})(name, {"pid": 1234}) == ""
```

Three consecutive lines assert that the tool is advertised to observe runs, that it is
outside the read-only allowlist, and that the observe policy admits it — the contradiction
was adjacent, in one test body, pinned. The same file, at line 26–27, already asserted the
other half:

```python
    general = server._agent_dispatch("process_list", {}, read_only=False)
    denied  = server._agent_dispatch("process_list", {}, read_only=True)
```

After, the same block asserts the denial instead (`... not in _AUTOPILOT_OBSERVE_TOOLS`,
`in _AUTOPILOT_WORKSPACE_TOOLS`, observe policy `.startswith("ERROR: HOST POLICY:")`,
`_agent_dispatch(..., read_only=True)` returns the read-only refusal).

`tests/test_tool_capabilities.py::test_local_read_only_project_dedup_and_autopilot_sets_are_unchanged`
changed `assert names - non_work <= server._AUTOPILOT_OBSERVE_TOOLS` to exclude
`process_tools` and additionally assert `process_tools.isdisjoint(_AUTOPILOT_OBSERVE_TOOLS)`
plus `names - non_work <= _AUTOPILOT_WORKSPACE_TOOLS`. **Both tests encoded the defect as
the spec.** Confirmed.

**Executed proof of the admit-then-deny, against the pre-fix module:**

```
process_list                 advertised=True  _autopilot_tool_policy(observe) = ''   _agent_dispatch(read_only=True) = "ERROR: tool 'process_list' is not allowed by the repository ..."
process_memory_risk_inspect  advertised=True  _autopilot_tool_policy(observe) = ''   _agent_dispatch(read_only=True) = "ERROR: tool 'process_memory_risk_inspect' ..."
task_progress                advertised=True  _autopilot_tool_policy(observe) = ''   _agent_dispatch(read_only=True) = "ERROR: tool 'task_progress' ..."
```

Gate order verified in source, not assumed: in `_agent_impl` (`server.py:16083`) the
policy hook runs first (`server.py:16394`, `server.py:16668`:
`policy_error = str(tool_policy(tool_name, policy_tool_args) or "")`) and the read-only
gate runs after (`server.py:16398`, `server.py:16688`:
`policy_error = _repository_read_only_error(...)`, defined at `server.py:13677`). The
observe run reaches both because `server.py:17516` passes
`read_only=(run.get("policy") == "observe" and not unsafe)` — and `unsafe_lab.active()`
is `False`, so the default observe path is read-only. **The admit-then-deny is real.**

**Does the new subset assertion bind? Yes, in both directions** (mutations 8 and 8b):
restoring the three names fails `test_autopilot_observe_allowlist_survives_repository_
read_only_policy` *and* both updated old tests; emptying the observe set fails
`test_extractors_cannot_go_vacuous` (per-surface floor of ≥30 names), so it cannot pass
vacuously.

### Sweep for the same shape elsewhere — this is the important part

The fixed defect is one instance of a general shape: **`_agent_impl` filters its
advertising surface on some host parameters and not others.** The gate chain in
`_agent_impl`'s tool loop, re-resolved and read in order, is:

| # | Gate | `server.py` | Denies by |
|---|---|---|---|
| 1 | `tool_allowlist` membership | 16661 | name |
| 2 | `tool_policy(...)` (e.g. `_autopilot_tool_policy`) | 16668 | name |
| 3 | `cloud` → `_cloud_agent_tool_policy_error` | 16670 | **name, unconditional** |
| 4 | `project_scope` → `_repository_scope_path_error` | 16674 | argument values |
| 5 | `project_scope` → `tool_name not in _PROJECT_BOUND_AGENT_TOOLS` | **16677** | **name, unconditional** |
| 6 | `project_scope` → `_agent_project_execution_argument_error` | 16683 | argument values |
| 7 | `read_only` → `_repository_read_only_error` | 16687 | name — *the gate in the fixed defect* |

`_agent_tool_help` (`server.py:13371`) filters on exactly **`read_only`, `cloud`, `unsafe`**
— those are its only three parameters. It has **no `project`, no `allow_web`, no
`allow_location`**. So gates 5, and the `allow_web`/`allow_location` denials inside
`_agent_dispatch`, are invisible to every advertising surface in the codebase. Four more
live instances follow, all **pre-existing** (none introduced by `277fd27`), all verified
here by computed set difference plus executed gate calls.

**F1 — `_AUTOPILOT_WORKSPACE_TOOLS` vs the project-bound gate. The direct sibling of the
bug just fixed.** `_AUTOPILOT_WORKSPACE_TOOLS - _PROJECT_BOUND_AGENT_TOOLS` =
`['artifact_generate', 'game_generate_and_test', 'game_reference_suite', 'run_code',
'run_project']` (computed). A `autopilot_start(..., project=...)` run at default
`policy="workspace"` renders all 63 names into the transcript as `HOST TOOL ALLOWLIST
(cannot be expanded by the model)` (`server.py:16264`) and `Allowed tools:`
(`server.py:17311`), is admitted at gates 1 and 2, and dies at gate 5 for those five.
Identical structure to the three tools this commit moved — one gate later in the same
chain. The block comment `277fd27` added at `server.py:17097-17104` states the invariant
as "*keep both sets a subset of `_agent_dispatch`'s branches and the observe set a subset
of `REPOSITORY_READ_ONLY_TOOLS`*" — it does not mention `_PROJECT_BOUND_AGENT_TOOLS`, and
neither does any test. `_AUTOPILOT_OBSERVE_TOOLS - _PROJECT_BOUND_AGENT_TOOLS` = `[]`, so
only the workspace set is affected.

**F2 — `AGENT_TOOL_HELP` vs the project-bound gate: 21 dead names.**
`advertised(AGENT_TOOL_HELP) - _PROJECT_BOUND_AGENT_TOOLS` = 21 names (computed):
`apply_learned, artifact_generate, game_generate_and_test, game_generation_campaign,
game_reference_suite, learn_preference, master_cancel, master_orchestrate, master_retry,
memory_embedding_backfill, memory_interaction_embedding_backfill, memory_privacy_repair,
memory_quality_repair, offload, run_code, run_project, self_heal_repair, set_context_size,
tune_emotion_vectors, update_emotion_vectors, workflow_run`. Any `agent(prompt,
project=...)` or `workbench_agent(...)` run gets the unfiltered help at `server.py:16254`
and then gate 5. `REPOSITORY_AGENT_TOOL_HELP - _PROJECT_BOUND_AGENT_TOOLS = []`, so the
repository help is clean — it is only the full-agent help. Note this is a *different* 21
from the parked 23, and `_KNOWN_UNDISPATCHABLE_HELP_ENTRIES` does not cover it.

**F3 — the negative-claim reviewer names three tools, and a hosted run denies all three.**
The reviewer system prompt (`server.py:14105-14110`) instructs the model that "*continue
must return exactly one structured read-only evidence action using `text_search`,
`file_read_range`, or `file_find`*", and the response schema repeats those three. The
action is admitted against `_AGENT_CLAIM_REVIEW_TOOLS` at `server.py:16386`, then hits
`if not policy_error and cloud: policy_error = _cloud_agent_tool_policy_error(tool_name)`
at `server.py:16395-16396`. Computed: `_AGENT_CLAIM_REVIEW_TOOLS ∩
_CLOUD_AGENT_LOCAL_ONLY_TOOLS` = `['file_find', 'file_read_range', 'text_search']` —
**all three, i.e. 100 % of the reviewer's advertised vocabulary.** Executed:
`_cloud_agent_tool_policy_error("text_search")` returns `ERROR: HOST POLICY: local-only
tool 'text_search' is disabled inside a hosted agent…`. `cloud` is passed straight through
at `server.py:16541` (`_agent_negative_claim_review(..., cloud=cloud)`) with no gating, so
on any hosted-tier run the entire `continue` branch of negative-claim review can only ever
emit a policy error. The two claim-review tools that *would* survive the cloud gate
(`project_detect`, `repository_symbol_index`) are the two the prompt never names — the
drift runs in both directions on one surface. This is a *verification* mechanism that
cannot execute on hosted runs; whether it then fails open or closed I did **not**
establish, so I am filing it Important rather than Critical, flagged for that follow-up.
The correct pattern already exists 3 000 lines earlier: `_agent_tool_help(cloud=True)`
(`server.py:13371`) derives its filter *from* `_cloud_agent_tool_policy_error` so the two
cannot drift; the reviewer prompt is a hardcoded string that never got that treatment.

**F4 — `allow_web=False` is invisible to every advertising surface.** `_agent_dispatch`
denies by name at `server.py:14304/14308/14312/14320`; executed:
`_agent_dispatch("web_search", …, allow_web=False)` → `ERROR: web access disabled for this
agent run` (same for `web_fetch`, `weather_lookup`). `_orchestrator_agent_worker`
(`server.py:6858-6866`) **hardcodes** `allow_web=False, read_only=True` and renders
`REPOSITORY_AGENT_TOOL_HELP`, which advertises exactly `['weather_lookup', 'web_fetch',
'web_search']` — dead on 100 % of `master_orchestrate` repository-worker runs, with no
configuration that could make them live. `autopilot_start(allow_web=False)` is the same
story for the work model (the *planner* prompt says `Web: off` at `server.py:17310`; the
work model, which is the one that calls tools, is not told).

**F5 (Minor) — `allow_location`.** `_agent_impl`'s default is `allow_location=False`
(verified from the signature). The research-agent call at `server.py:12226-12235` passes
`tool_allowlist=("web_search","web_fetch","weather_lookup","approximate_location_lookup")`
and never passes `allow_location`, so one of its four advertised names is always denied;
executed: `ERROR: approximate location requires host-verified user consent for this agent
run`. On the generic `agent()` path the operator can pass `allow_location=True`, so that
surface is caller-varied and not an instance.

**Discarded, with computed evidence** (each fails at least one of: is it advertised / is
the gate on the same path after it / is the denial name-unconditional):

* `_AUTOPILOT_WORKSPACE_TOOLS - REPOSITORY_READ_ONLY_TOOLS` = 26 names — looks like the
  fixed bug at 8× scale, **is not**: `server.py:17516` sets `read_only` **False** for any
  policy other than `observe`, so gate 7 is never reached. Discarded on the call path, not
  on the set difference.
* `REPOSITORY_READ_ONLY_TOOLS - dispatch` = `[]`; `_AUTOPILOT_*_TOOLS - dispatch` = `[]`
  (now asserted); `advertised(REPOSITORY_AGENT_TOOL_HELP)` vs `REPOSITORY_READ_ONLY_TOOLS`
  = `[]` both directions; `_AGENT_DEDUPLICATED_INSPECTION_TOOLS - dispatch` = `[]`;
  `_SPECULATABLE_ARGFREE_TOOLS` clean against both gates.
* `advertised(_agent_tool_help(cloud=True))` ∩ `_CLOUD_AGENT_LOCAL_ONLY_TOOLS` = `[]` for
  every flag combination — correct **by construction**, because that filter is derived
  from the policy function. This is the pattern F3 should have followed.
* `_repository_scope_path_error` (13424), `_agent_project_execution_argument_error`
  (13549), `REPOSITORY_READ_ONLY_FORBIDDEN_ARGS` (13286), `_GIT_IGNORE_DISCOVERY_TOOLS` —
  deny on **argument values**, not names. Not this shape.
* `_WORK_MUTATION_TOOLS`, `_WORK_VALIDATION_TOOLS`, `_WORK_INSPECTION_TOOLS`,
  `_AGENT_FILE_EVIDENCE_TOOLS`, `_PROJECT_SCOPED_PATH_TOOLS` — evidence/receipt
  classifiers, never rendered to a model, so not advertised. They do each carry members of
  the undispatchable 23, which is why those names keep resurfacing; advertising any of
  these sets later would import the defect wholesale.
* `_loop_dispatch` — applies **no** per-action policy gate; it calls the registered tools
  directly, so there is no later gate for an admission to be dead against. No finding.
* `permission_modes.py` / `permission_rules.py` — **different shape, worth its own item**:
  `permission_modes.decide` and `permission_rules.check` are never called on any dispatch
  path (`server.py` uses only `format_policy`/`add_rule`/`set_mode`/`overview` for
  display), so there is no admit→deny pair because there is no enforcement. Separately,
  `permission_modes.py:128` is `PRIVILEGED_TOOLS = frozenset()` — empty, so the elevation
  branch of `_MATRIX` can never be selected. That is the *guard that silently no-ops*
  shape, not this one.

**Structural recommendation.** `test_agent_help_dispatch_drift.py` and the new
`test_advertised_surface_drift.py` both auto-enumerate the **bool-defaulted parameters** of
`_agent_tool_help` via `itertools.product`. Giving `_agent_tool_help` a
`project_bound: bool = False` (filtering from `_PROJECT_BOUND_AGENT_TOOLS`) and
`allow_web: bool = True` / `allow_location: bool = False` — the same derive-from-policy
trick already used for `cloud` — would fix F2/F4/F5 *and* make both existing guards cover
them automatically, with no new test code.

---

## 6. Accounting

* **RED reproduced exactly.** Running the committed guard file against pre-fix `server.py`
  (pinned via `sys.path`, `server.__file__` asserted to be the scratch copy) gives
  `8 failed, 2 passed in 0.61s` — the **same eight test IDs** the report lists.
* **GREEN reproduced:** `10 passed in 1.91s`. `--collect-only` reports **10 tests
  collected**, so `8 + 2 = 10` reconciles and RED was measured at the final item count —
  no test was added after the RED run.
* **Scoped regression reproduced:** the 49 named files give `912 passed, 1 skipped,
  1 warning in 62.80s` (report: 56.97s; same counts). All 49 exist; the list has no
  duplicates.
* **The 49-file set does not cover everything it should — a caveat, not a failure.** The
  report describes the selection as "*every test file in `tests/` that references
  `_AUTOPILOT_*`, `tool_manifest`, `AGENT_TOOL_HELP`, `_loop_dispatch` or `workflow`*".
  Recomputing that rule over all 279 test files selects **61**, not 49; the 49 are a
  subset. More importantly the *rule itself* was keyed to the surfaces that were
  **measured**, not the ones that were **changed** — so it never selects
  `tests/test_read_only_agent_policy.py`, even though this change moves three tools across
  exactly that gate. Also missed: `test_process_risk.py` (process tools),
  `test_git_ignore_privacy.py` (`_autopilot_tool_policy`), `test_repl_catalog.py`
  (`task_progress`), and eight `test_harness_*` files covering the 23 dropped tools.
  I ran the 16 relevant excluded files: **339 passed, 7 skipped**. So the 912 is a genuine
  pass rather than a floor — but it was luck, not coverage, and
  `test_read_only_agent_policy.py` is one of the four files that catch mutation 4.
* **The full suite was NOT run** — explicitly, per the constraint. 279 test files exist;
  49 + 16 = 65 were exercised here.

---

## 7. Findings

| Sev | Finding |
|---|---|
| **Important (new)** | The alias allowance in `test_advertised_surface_drift.py:222` subtracts alias **keys** while `test_agent_tool_aliases_all_resolve_to_registered_tools` only validates alias **targets**. A name backed by no `@mcp.tool()` and no dispatch branch can be advertised on `tool_manifest()` and **no test in the repo fails** (mutation 7: 45 passed). The test's own docstring and report §3 both claim the opposite. Fix: assert `set(_AGENT_TOOL_ALIASES) <= dispatch_names(_agent_dispatch)`; all nine current keys already comply. |
| **Minor (new)** | Report §2 and §5 state "46 registered tools are absent from `tool_manifest()`" in the present tense. 46 is the pre-fix value; HEAD is **43**. A figure measured at one revision reported as a fact about another — this family's signature error. |
| **Minor (new)** | The `#22 = 0` defence ("the same extractor found 3 on `tool_manifest()`") is factually wrong: `_help_advertised` and `_manifest_advertised` are disjoint parsers, each yielding 0 on the other's surface. The conclusion is right; the stated reason is not, and should be replaced with the greedy-token-sweep evidence in §2.2. |
| **Minor (new)** | `REPOSITORY_READ_ONLY_TOOLS` is checked for registration but not dispatchability, so `b8a15ef`'s own defect re-added there passes this guard 10/10 (mutation 4). Caught by four older tests, and the missing assertion is currently satisfiable (gap 0). One-word fix at line 246. |
| **Minor (new)** | The scoped regression's selection rule keys on measured surfaces, not changed ones, and misses `tests/test_read_only_agent_policy.py`. Those files pass (339 passed, 7 skipped), so nothing is hidden — but the rule should be re-derived from the diff. |
| **Minor (new)** | Report §3 says the guard covers "all **four** `_agent_tool_help` flag combinations". `_agent_tool_help` has three bool parameters (`read_only`, `cloud`, `unsafe`), so it is **eight**. Harmless understatement, but another restated-not-measured number. |

### Findings from the admit-then-deny sweep (all **pre-existing**; none introduced by `277fd27`)

| Sev | Finding |
|---|---|
| **Important (new)** | **F3** — the negative-claim reviewer's prompt (`server.py:14105-14110`) names `text_search`, `file_read_range`, `file_find`; all three are in `_CLOUD_AGENT_LOCAL_ONLY_TOOLS` and are denied at `server.py:16395` on any hosted run (executed). 100 % of the reviewer's advertised vocabulary is dead on cloud tiers, while the two claim-review tools that *would* survive are the two the prompt never names. A verification mechanism that cannot execute. **Critical candidate** pending a check of whether it then fails open. |
| **Important (new)** | **F1** — `_AUTOPILOT_WORKSPACE_TOOLS - _PROJECT_BOUND_AGENT_TOOLS` = `artifact_generate, game_generate_and_test, game_reference_suite, run_code, run_project`: five names rendered into the `HOST TOOL ALLOWLIST`, admitted at gates 1–2, denied at gate 5 (`server.py:16677`) for any project-bound workspace autopilot run. The literal sibling of the bug this commit fixed, one gate further down the same chain, and not mentioned in the invariant comment `277fd27` added. |
| **Important (new)** | **F2** — 21 names in `AGENT_TOOL_HELP` are outside `_PROJECT_BOUND_AGENT_TOOLS` and die at the same gate 5 on every `agent(project=…)` / `workbench_agent` run. Distinct from the parked 23; `_KNOWN_UNDISPATCHABLE_HELP_ENTRIES` does not cover them. |
| **Important (new)** | **F4** — `_orchestrator_agent_worker` (`server.py:6858-6866`) hardcodes `allow_web=False` while rendering `REPOSITORY_AGENT_TOOL_HELP`, which advertises `web_search`, `web_fetch`, `weather_lookup`. Dead on 100 % of `master_orchestrate` repository-worker runs, with no setting that could make them live (executed denial). |
| **Minor (new)** | **F5** — `server.py:12226-12235` advertises `approximate_location_lookup` in a hardcoded 4-tool allowlist without passing `allow_location`, whose default is `False`. 1 of 4 always denied (executed). |
| **Minor (new, different shape)** | `permission_modes.decide` / `permission_rules.check` are never called on any dispatch path, and `permission_modes.py:128` is `PRIVILEGED_TOOLS = frozenset()` — an empty set gating the elevation branch of `_MATRIX`. A guard that silently no-ops; separate item. |
| — | No new Critical confirmed. No incorrect production change found in `277fd27`. |

**The generalisable finding:** `_agent_tool_help` filters on `read_only`, `cloud` and
`unsafe` and on nothing else, while `_agent_impl` has three further name-unconditional
denial gates keyed on `project_scope`, `allow_web` and `allow_location`. Every guard in
the repo — including this lane's new one — is built on the first three dimensions, so the
last three are unmeasured everywhere. The three tools this commit fixed were the instance
that happened to fall on a covered dimension.

## 8. Verdict

**MERGE**, with the alias-laundering Important fixed or logged (a single added assertion,
no production impact), and F1–F5 filed as new items for the next lane.

`277fd27` is correct as it stands. Everything below the merge line is either a defect in
the *report's prose* (three restated-not-measured numbers, one invalid defence) or a
**pre-existing** defect the sweep surfaced, not a defect in the shipped code. The one
issue that touches this commit's own deliverable is the alias-laundering hole in the new
guard, and it is additive to fix.

The re-measurement method survives audit: the 184 figure holds under a third structurally
independent method, `_agent_dispatch`'s no-fallthrough property is confirmed by execution,
the per-branch grouping is the correct unit and its 10-vs-27 correction reproduces
exactly, and every pre-fix and post-fix figure in the report reproduces except the `46`
(now 43) and the "four flag combinations" (eight). The `#22 = 0` result is right for
reasons the report gets wrong. The production changes are sound, the guard binds on all
three of the lane's mutations and on the exact regression it was written for, and it
cannot pass vacuously. Its blind spots are narrow and all but one are covered by older
tests; the exception is the `tool_manifest()` laundering route.

The sweep asked for in question E is where the real value is, and it is not empty: four
more live admit-then-deny surfaces exist, on the three host dimensions
(`project_scope`, `allow_web`, `allow_location`) that no advertising surface and no guard
in this repo has ever filtered on.

Checkout left clean on `work/13-drift-family`; no file in this worktree or in
`D:\sonder-wt\12-merge-dispatch` was modified; `git stash` was never run and
`stash@{0}`/`stash@{1}` are intact.
