# Sweep of the fleet's own output

Read-only sweep of 20 branches against `9f377f1`. Nothing outside this file was
modified. No `git stash`, no `git add -A`, no checkouts, no sibling worktrees
touched.

## Lineage — the brief was wrong, verified with `merge-base --is-ancestor`

All 20 branches descend from `9f377f1`. But the brief's claim that this branch
descends from "drift-family -> dead-vocab -> standing/plan-mode" is **false as
stated**. HEAD (`f5ba52a`) contains only *prefixes* of those lanes:

| branch | in HEAD? | what HEAD is missing |
|---|---|---|
| `fix/cloud-help-drift` | yes (`b8a15ef`) | — |
| `work/13-drift-family` | **prefix only** (to `dad98ac`) | `278839e` alias-key laundering fix, `c418c3e` |
| `work/16-dead-vocab` | **prefix only** (to `746d18b`) | `dfad7ce` ERROR-signal ratchet fix, `42877c4` |
| `work/20-standing-planmode` | yes — HEAD *is* `work/20` | — |

Both omissions are live defects on HEAD (findings 3 and 8). Checking rather than
trusting is what produced them.

Brief claims that *did* hold: HEAD has no permission-gate wiring
(`permission_modes.py` is byte-identical to base and has **zero** production
call sites), and `harness_tools._resolve_root` on HEAD does no confinement
(HEAD's own test asserts `"allowed" not in inspect.getsource(_resolve_root)`).

## Coverage

**Method.** Scripted scan of every added line across all 20 branch diffs
(~60k insertions, 83 changed files) for the four shape signatures, then read the
candidates. Four parallel agents on the large lanes; I took the permission-gate
lane and the drift/dead-vocab/prompt-fence/standing lanes. Findings were verified
by execution where executable — an isolated `permission_modes` harness, a pytest
plugin injecting a ghost alias, and the repo's own ratchet checker.

**Reached and read:** `permission_modes.py` (full), `command_catalog.py`
(catalog/by_name/CatalogUnavailable), `sonder_serve.py` (gate region),
`sonder_repl.py`, `reloadable_mcp.py`, `orchestrator.py` (fact selection + fence),
`scripts/select_regression_tests.py` (full), `harness_tools.py`,
`retriever.py`, `memory_store.py`, `grounded_outcomes.py`, `learning_health.py`,
`reward.py`, `memory_quality.py`, `codegen_loop.py`, `json_schema_verifier.py`,
`tool_capabilities.py`, `calibration.py`, `export_training_data.py`,
`activity_tracker.py`, `sonder_runtime/domain/memory/rules.py`, both benchmark
scripts, and the `server.py` regions each lane touched.

**NOT reached (honest blind spots):**
- `proposals/metrics_report/*` — changed by 4 lanes, read by no one.
- `sonder_runtime/domain/execution/policy.py` (`feat/cot-opt-in`) — not read.
- `command_registry.py` — grep only.
- `grounded_extraction.py` (`sdd/03-schema-offload`) — production not read.
- `sonder_repl.py` (+215 on `work/12`) and `sonder_serve.py` (+111) — read on
  `sdd/01` for the gate region, not on `work/12`.
- Bodies of `test_agent_verification_gate.py`, `test_end_report_standing.py`,
  `test_permission_policy_display.py`, `test_grounded_outcomes_agent_dispatch.py`
  — targeted grep only.

`work/12-merge-dispatch` **was** reached (it finished last): `harness_tools.py`,
`permission_modes.py`, `permission_rules.py`, `command_registry.py`,
`command_catalog.py`, `reloadable_mcp.py`, the `server.py` gate/dispatch/scoping
regions, plus an AST sweep of `_agent_dispatch`, a zero-caller sweep over all 76
new definitions, and two live probes against the branch's `harness_tools`.

## Verified findings

### 0. HIGH — a write-enabled project-bound agent run cannot use any of the 23 developer tools it was just given
`server.py:15942`, `_agent_dispatch_observed`, `work/12-merge-dispatch`.

The two arms are asymmetric:

```python
if read_only:
    observation = _agent_dispatch(tool_name, dispatch_args, read_only=True,
                                  repository_extra_roots=project, **dispatch_options)
else:
    observation = _agent_dispatch(tool_name, dispatch_args, **dispatch_options)
```

The write-enabled arm passes no `repository_extra_roots`, so the run opens
`authorized_root_scope("")` and every tool routed through `_resolve_root` refuses
the bound project. Probed live on the branch: with the scope open on the project,
`test_discover` returns 1 test; with `""` — the write path — `test_discover`,
`build_run` and `git_commit` all raise
`PermissionError: root is outside every authorized root: <project>`.

The commit that wired these tools in breaks them for the exact run type that
mutates. **This shipped green because of a test passing for a neighbouring
reason:** every write-path test in `tests/test_agent_dispatch_dev_tools.py`
(143-150, 173, 191, 258) does `monkeypatch.setattr(server, tool_name, lambda ...)`,
so `_resolve_root` is never reached; the only end-to-end control,
`test_harness_root_confinement.py::test_a_host_selected_project_root_still_reaches_the_tool`,
uses `read_only=True`. Confinement has **no write-path coverage at all**.

### 1. HIGH — the completion gate's new second route can be satisfied by a verifier that exercised nothing
`server.py` `_agent_verification_covers` (16125) -> `_work_validated` (17118),
on `work/17-selfmod-gate` and `work/18-riskof-location`.

`_work_validated = validation_ok or verification_ok` adds a second way to pass
the gate. Its coverage check reads **only `args["root"]`**, never `args["path"]`,
and its docstring justifies that: *"their `path` argument narrows which checks
run inside it, not what those checks exercise."* **The same file contradicts this
1,500 lines earlier** (15655-15658): *"a second `path` argument which
harness_tools appends straight to the child argv ... so `lint_run(path="../../x")`
would write outside the project."*

Production path (agent lane, `auto_checklist=True`): model calls
`file_write(path="<proj>/payments.py")` -> `mutated=True`, `validation_ok=False`.
Model then calls `build_run(root="<proj>", command="git --version")`.
`harness_tools.build_run` (727-733) forwards the caller-supplied `command` via
`command.split()`; exit 0 => `tool_ok=True`. `_agent_verification_covers` sees
`payments.py` inside `<proj>` and returns True => `verification_ok=True` =>
`validation_failed=False`. The `VALIDATION_FAILED` line is suppressed, the
checklist reads *"grounded validation passed"*, `HostTaskResult.validation_passed`
is True, and `autopilot_controller._task_passed` accepts a whole `validate` task.
Nothing compiled, linted or tested the changed file.

The older sibling `_agent_validation_covers` (17781) *does* read `path` and emits
`"HOST VALIDATION: this check did not cover the changed on-disk path(s)"`, so the
new route is strictly weaker than the one it is declared equivalent to.

**Guard that cannot fail:** `tests/test_agent_verification_gate.py` tests at 551
and 254 vary only `root`. No test supplies a narrowing `path` or a trivial
`build_run` command, so no input in the suite can fail the check for the reason
it claims to enforce.

### 1b. HIGH — the other half of the same fact: `path` is appended to child argv unconfined
`harness_tools.py:411` `lint_run`, and identically `format_code`, `test_run`,
`typecheck_run`, on `work/12-merge-dispatch`.

`_resolve_root` confines `root`; `path` is then `cmd.append(path)` with no check.
`lint_run(root=<authorized>, path="../../../Users/natew/Documents", fix=True)`
runs `ruff check --fix` on that target from `cwd=root` — a **write outside every
authorized root**. `_repository_scope_path_error` (`server.py:17538`) is the only
guard and it returns `""` when `project_scope` is empty, so an unbound agent run
and every direct `@mcp.tool()` client are unprotected.

The branch documents the hazard itself at `server.py:15659` — *"Containing `root`
alone leaves `path` checked by nothing"* — and closes it only on the
project-bound path. `_resolve_root`'s "one layer below every entry point" claim
covers the working directory, not the target.

**Read finding 1 and this one together:** `_agent_verification_covers` justifies
ignoring `path` on the grounds that it "narrows which checks run inside it, not
what those checks exercise", while this code proves `path` is load-bearing and
unguarded. One fact, contradicted in two directions, in the same lane.

### 2. HIGH — the new fail-closed guard in `risk_of` is bypassed by the `except Exception` two lines below it
`permission_modes.py` `risk_of` (500-511), `sdd/01-permission-gate`.

At base, `risk_of` had one `try: ... except Exception:`. The branch **split** it,
adding `except CatalogUnavailable: return "dangerous"` while **retaining** the
broad `except Exception: command = None`, which falls through to
`return catalogued or "ask"`. So a blind classifier that raises anything other
than `CatalogUnavailable` still reclassifies dangerous tools as `ask`.

`catalog()` converts only the registry read into `CatalogUnavailable`; the
remaining ~100 lines (`import command_registry`, `_native_groups()`, `Command`
construction, `_category_for`) are unwrapped, so an `ImportError`/`AttributeError`
during a partially-initialised server — the exact scenario `CatalogUnavailable`'s
own docstring cites — takes the unwrapped path.

Verified by execution (isolated harness):

```
classifier blind via catalog_unavailable: risk_of('git_merge') = 'dangerous'
    console operator present, mode=acceptEdits  -> ask
    console operator present, mode=auto         -> ask
classifier blind via other_exception:      risk_of('git_merge') = 'ask'
    console operator present, mode=acceptEdits  -> allow      <-- guard defeated
    console operator present, mode=auto         -> allow      <-- guard defeated
```

`git_merge`, `sqlite_mutate`, `task_delete`, `permission_rule_set` run outright
with an operator sitting there — the precise defect commit `7cb5052` was written
to fix, reintroduced beside the fix.

### 3. HIGH — the alias-key laundering fix exists on one branch; three others carry the hole plus its false docstring
`tests/test_advertised_surface_drift.py`.

`278839e` (on `work/13-drift-family`) closed a route where the allowance
`names - registered - aliases` subtracts alias **keys** while the paired test only
validated alias **targets**. `work/16-dead-vocab`, `work/20-standing-planmode`
and **HEAD** carry the pre-fix version — including the docstring that still
claims *"The alias allowance above must not be able to launder a fake name."*

Reproduced on HEAD: advertise an unregistered `__ghost_manifest__` on
`tool_manifest()` and add `_AGENT_TOOL_ALIASES["__ghost_manifest__"] =
"memory_search"` -> **21/21 guard tests pass**. Same injection against
`work/13`'s file -> **fails**, naming `['__ghost_manifest__']`.

Merge hazard: the two files diverge by 232 changed lines and `git merge-tree`
reports a conflict, so the 23-line fix is on the losing side of a manual
resolution — "take theirs" silently drops it.

### 4. HIGH — the quarantine gate still reads the blended reward (shape B half-fix)
`retriever.py` `lesson_quarantine` (415, 437) gates on `avg_reward_since_win`,
computed in `memory_store.lesson_usage_stats.finish()` (2117) as a plain mean over
`rewards_since_win`, appended at 2142 with **no `outcome_signal` filter** — it
blends a caller's `rejected` (-0.5) with the runtime's own `failed` (-1.0).

`fix/data-layer-residuals` de-blended the *weak* consumer (`_usage_boost`, a
±0.01 tiebreak) and wrote in `memory_store.py` that `avg_reward` "must never
ORDER two lessons", but left the *hard* consumer untouched. Misfire: five
self-graded `failed` rows (mean -1.0 <= `QUARANTINE_MAX_AVG_REWARD` = -0.5) with
**zero caller judgement** evict a lesson from retrieval entirely, and the same
decision feeds `learning_health.py:318` -> `_status` "watch". Symmetrically, a
real caller `rejected` diluted by execution rows escapes the gate.

`work/15-codegen-loop` independently splits the same stat into
`avg_reward_caller`/`avg_reward_execution` and fixes only
`memory_quality.choose_exact_duplicate_keeper`, leaving `retriever._usage_boost`
(507-521, feeding the sort key at 595 and 608) reading `avg_reward`.

### 5. HIGH — the "unmeasured, not failed" guard reaches 4 of 11 verifiers
`grounded_outcomes.attribute(..., evidence=)` +
`evaluation_infrastructure_error()`. In `server.py`, `evidence=<data>` appears at
8 sites = **4 tools** (`test_run`, `lint_run`, `typecheck_run`, `build_run`, at
9676/9686, 9710/9718, 9767/9775, 10050/10057). The other 7 members of
`grounded_outcomes.VERIFIERS` — `run_code`, `run_project`, `isolated_run`,
`codegen_build_loop`, `artifact_verify`, `ground_artifact`, `artifact_ground` —
call `_record_direct_tool(...)` with no `evidence=`.

`evaluation_infrastructure_error(None)` returns `""` by its documented
"silence when there is no evidence" branch, so `attribute()` proceeds. Misfire: a
`run_project` build exceeding `timeout` raises `TimeoutExpired` -> `ok=False` ->
signal `failed` (**reward -1.0, harshest in the table**) against a generation
nothing examined, *and* it consumes that generation's one-shot pending entry, so
the later real verification returns `unlinked`. Found independently by two agents.

Cross-branch: `sdd/02-calibration` adds a *new* `attribute()` call site
(`server.py:15620` -> `_feed_grounded_outcome` at 7802) that also has no
`evidence`, so on merge that whole lane bypasses the guard too.

### 6. HIGH — the regression selector's default mode silently drops all committed work
`scripts/select_regression_tests.py` `changed_diff()` (77-86). The docstring says
*"Committed-but-unpushed work plus anything still in the working tree."* The code
sets `upstream = rev-parse --verify --quiet HEAD` — HEAD's **own** sha — and uses
it purely as a truthiness test, then returns `git diff -U0 HEAD`. No commit range
is ever diffed. Reproduced in a throwaway repo: one committed + one uncommitted
module -> `selected 1 of 2 test files ... across 1 module(s): mod_b`, exit 0, no
warning; the committed module is invisible.

The VACUOUS guard (exit 2) fires only on a fully clean tree — confirmed on this
branch, which exits 2 — so the dangerous case is precisely a partially-dirty tree,
which is what every lane had while working.

### 7. MEDIUM — the "selected N of M" number is a token artifact, not coverage
Same file. Selection is a name heuristic: module-level symbols from the diff,
text-matched against test bodies. `STOPWORDS` (57-64) omits `check`, `validate`,
`main`, `run`, `score`, `report`, `status`. Measured: `fix/schema-verifier-widen`
"81 of 310" — **69 of the 81 come from the single word `check`**, 19 from
`validate`; only 1 file mentions `json_schema_verify`, and 25 of 34 changed
identifiers are named by zero tests. Likewise `main` = 56 of 116
(`sdd/03-schema-offload`), `score` = 24 of 71 (`work/15`), `_run` = 13 of 27
(`work/14`). On this branch `--since 9f377f1` selects **232 of 315** (74% of the
suite) off generic tokens like `main`, `report`, `status`, `loop`, `agent`.

The tool is honest about this — it prints the uncovered inverse ("17 changed
identifier(s) NO test file mentions") and its docstring says the selected set
"means nothing if the specific thing you changed is in the uncovered list". The
defect is lanes quoting the selected count as evidence. **That is a floor.**

### 8. MEDIUM — HEAD ships RED on the repo's own shrink-only ratchet
`scripts/check_error_signals.py` exits **1** on HEAD (= `work/20-standing-planmode`)
with 4 `return_literal_prefix` findings in `_agent_run_tool_policy_error`
(`server.py:15290, 15295, 15300, 15302`). The fix (`dfad7ce`, renaming it to
`_agent_run_tool_refusal` and returning short gate names) exists **only** on
`work/16-dead-vocab`, which is clean.

*(Method note: my first reading of this exit code was `tail`'s, not the
checker's. Re-run without the pipe to get the real 1.)*

### 9. MEDIUM — `risk_of`'s fail-closed is a no-op at all five non-interactive call sites
The `CatalogUnavailable -> "dangerous"` guard's own comment says continuing would
let *"a caller with nobody to ask resolve to allow — a gate that cannot see
refusing nothing."* But `dangerous` maps to `ASK` in every non-plan mode, and
`ASK` + `interactive=False` degrades to `ALLOW`. Counterfactual table (guard
present vs absent), computed from the real `_MATRIX`:

```
differing cells = 2 (both interactive)   identical cells = 6
non-interactive rows (the 5 production call sites): guard changes NOTHING
```

The guard only bites at the interactive console in `acceptEdits`/`auto` — and
finding 2 defeats it even there. Tempered: the module's `ASK_CAVEAT` and the
guard's test docstring both disclose the degrade honestly, so this misleads a
reader of the guard's own comment rather than creating a new hole.

### 9b. MEDIUM — `_resolve_root` answers "does this exist?" before "are you allowed?"
`harness_tools.py:68-71`, `work/12-merge-dispatch`:

```python
p = Path(root or ".").resolve()
if not p.is_dir():
    raise ValueError("not a directory: %s" % p)
_require_authorized_root(p, extra_roots)
```

The existence check precedes authorization, and both messages echo the **resolved
host path**. Probed live: an unauthorized *missing* path yields
`ValueError: not a directory: C:\Users\natew\__definitely_not_here__`; an
unauthorized *existing* one yields
`PermissionError: root is outside every authorized root: C:\Windows\System32\drivers`.
Every server wrapper does `return "ERROR: %s" % exc` (e.g. `server.py:9881`), so a
confined agent gets a directory-existence oracle over the whole filesystem plus
path disclosure — the same class of leak this very commit fixed in `diff_files`.

### 9c. MEDIUM — the gate's risk authority cannot be refreshed at runtime
`command_catalog.reset_cache()` has **zero production callers** (tests only) — the
A3 shape, recurring for a third time. This branch nonetheless added
`console_tools`/`http_slash_tools`/`_module_level_functions` `cache_clear()` to it
and made `catalog()` the permission gate's risk authority. `command_registry` and
`permission_rules` are in `LIVE_RELOAD_MODULES` (`server.py:569`);
`command_catalog` is **not**. Consequences: the four re-grades this branch makes
(`/training`, `/selfmod`, `/mcp` -> `dangerous`, `/goal` -> `mutation`) take effect
only after a restart, and a tool registered by `/mcp refresh` misses the catalog,
so `risk_of` falls through to `"ask"` — which `interactive=False` degrades to
`allow` (finding 9).

### 10. LOW-MEDIUM — the gate's "Enforcement scope" table is already stale on the branch that wrote it
`permission_modes.py` docstring (60-80) claims to enumerate *"Every place a tool
is chosen"* as five call sites, *"Three of the five ... pass interactive=False"*.
Actual on `sdd/01`: six surfaces, seven enforcement calls, **five** non-interactive.
`sonder_serve._http_tool_refusal` (958) — the HTTP gate, with two entry points —
is absent from the table entirely. A reader auditing gate coverage from the
canonical description misses the network-facing surface.

## Merge landmine

`tests/test_offload_schema.py:503`
`test_the_verifier_really_ignores_the_keywords_coverage_flags`
(`sdd/03-schema-offload`) asserts `validate(datum, schema) == []` for six plainly
violating artifacts (`enum`, `minimum`, `minLength`, `additionalProperties`,
`uniqueItems`, `pattern`). `fix/schema-verifier-widen` enforces all six. Landing
both turns it RED, and the cheap resolution is to re-weaken the verifier.

It is *not* a defect encoded as a requirement — it is an honest characterization
test whose comment explains the pairing — but it asserts a permissive outcome on
a path whose job is refusal, which is the signature, and it will pressure the
wrong fix on merge. Flagging it as a landmine, not an indictment.

## Guards that cannot fail (green on arrival, never mutated)

- `tests/test_agent_verification_gate.py` (551, 254) — vary only `root`; the
  `path` axis the guard claims to enforce is never exercised. (finding 1)
- `tests/test_json_schema_verifier.py:552`
  `test_a_checked_keyword_is_never_reported_as_a_coverage_gap` — a negative
  assertion; deleting the whole unchecked-keyword reporting block leaves it green.
- `tests/test_orchestrator_fact_hijack.py`
  `test_the_two_recall_canaries_survive_the_bound_verbatim` — ~480 chars against a
  4000/12 cap, so the bound it names provably never engages.
- `tests/test_benchmark_schema_offload.py`
  `..._names_a_truncated_arm_as_truncated` — its second assert evaluates the
  fragment `": **"`, which can never contain "improve".

Counter-example worth recording: `278839e` explicitly mutation-proved its new
assertion (ghost alias -> 1 failure; reverted -> green) *because* it was green on
arrival. That is the standard the rest of the fleet's new guards should meet.

## Tests encoding a defect as a requirement

**None verified.** Every permissive assertion examined turned out to pin
deliberate, documented behaviour with a stated rationale. Two candidates were
raised and both overturned on reading:

- `test_a_dangerous_tool_does_not_downgrade_when_the_catalog_is_blind` asserts
  only the two cells that hold and explicitly disclaims the non-interactive
  degrade in its docstring.
- `test_a_single_oversized_fact_is_never_cut_in_half` (`work/19`) pins the
  documented `first_fitting` fallback — *"a fact cut in half is a fact that lies,
  and a recall canary is a single fact"* — not the first-slot exemption, which
  `work/19` had already removed. **Overturned a subagent finding here.**

One borderline case, recorded as confirmation rather than a new finding:
`tests/test_permission_gate_dispatch.py:463`
`test_manual_allows_every_risk_class_on_the_agent_path` pins `file_write` and
`run_code` as **allowed** on the agent path, and `:431` pins that `manual` refuses
nothing but an explicit `DEFAULT_RULES` deny. That is a permissive assertion on a
path whose job is refusal — but it is the already-known, already-documented
`interactive=False` degrade being written down, not a newly smuggled hole. Worth
knowing that the degrade now has tests defending it: any future attempt to make
the agent path fail closed will have to delete these two tests, and their names
give no hint that they are guarding a deliberate weakening.

## Counts that are a floor

Findings 6 and 7 (the regression selector, both mechanisms).
Plus `harness_tools.secret_scan` (896-936): on the 100-finding cap it returns
`files_scanned: scanned` — files counted *so far* — with `ok: True`. The wrapper
prints a "(truncated at 100 findings)" note so a human can infer it, but
`files_scanned` reads as coverage and a programmatic consumer sees `ok=True`.
Pre-existing body; only the signature changed in range. Minor.

Explicitly clean on this axis: `codegen_loop.output_truncated` is fully wired;
`export_training_data` names every cap in `rejected_by_reason`;
`tool_capabilities.described_fraction` returns `None` on 0-of-0 rather than 100%;
`benchmark_schema_offload.aggregate` raises on unaccounted rows and refuses
cross-arm deltas at differing completion.

## Discarded candidates: 54

Mine (4): `orchestrator.select_facts` has no production caller but is a
deliberate test seam over `_draw_facts`, which *is* the production path — tests
exercise the real logic. `POST /v1/permission-mode` looked like an unauthenticated
mode switch but `do_POST` checks `context["authorized"]` first. `risk_of`'s
empty-name `return "ask"` is the same no-op shape as finding 9 and self-disclaims
as inert. The oversized-fact test, above.

Agents (50): 7 from the calibration lane (incl. `learning_health.
gating_positive_percent` — both readers handle `None`), 28 from the codegen lane
(incl. both known shape-3 templates, now genuinely remedied: the shrink-floor test
binds at ratio 0.245, well clear of the `<40`-byte rule, and the dual-role
extractor binds on `pending_count()==2`), 9 from the selfmod lane (incl. a full A3
census — **every one of the 81/79 new production definitions on `work/17`/`work/18`
has at least one non-test call site**), and 6 from the merge-dispatch lane.

Three of the merge-dispatch discards are worth recording as *cleared*, because
they are the checks a reader would want done:

- **Path traversal / symlink / UNC / drive-relative escape of `_resolve_root`:**
  no escape constructible. `resolve()` precedes the check, and `file_ops._is_inside`
  uses `normcase` + `normpath` + `commonpath` — **not** the classic
  `commonprefix` string-prefix bug.
- **`apply_patch` traversal via `patch_text`:** could not construct against
  `git apply`'s own refusal.
- **A3 sweep over all 76 new definitions on `work/12`:** only `_default_rule_lookup`
  came back, and it is wired by assignment (`_rule_lookup = _default_rule_lookup`),
  so it is not an orphan.

All discards share one reason: pattern matched, no still-passing production path
could be constructed.

## One latent item, recorded not filed

`learning_health._gating_positive_percent`: the `reviewed` bucket and
`calibration.CALLER_JUDGED` coincide exactly under the current 8-signal
vocabulary, and `_MIN_REVIEWED_SAMPLE` (20) == `calibration.MIN_SAMPLE` (20), so
display and threshold agree **today**. Adding any ninth signal outside both sets
puts it in `reviewed` and silently into the gated rate.
