# The standing the agent was never told, and what plan mode got wrong (#23, #19)

Worktree `D:\sonder-wt\20-standing-planmode`, branch `work/20-standing-planmode`,
parent `746d18b`, which sits on `feat/verified-fetch-modes-calibration` @
`9f377f1`. Nothing here is compared against `main`.

## 0. Two premises in the brief were false, and one of them governs #19

Checked first, because both change what the correct fix is.

| Brief says this branch carries | Reality (`git merge-base --is-ancestor`) |
|---|---|
| the permission gate wired into dispatch (`b47b3ca`) | **not an ancestor** |
| a completeness floor over the tool map (`5b3ae1a`) | **not an ancestor** |
| root confinement in `harness_tools._resolve_root` (`2cec327`) | **not an ancestor** |

`git log HEAD --not 9f377f1` returns exactly seven commits: `b8a15ef`,
`277fd27`, `dad98ac`, `8ad7306`, `3e27ae6`, `2555cae`, `746d18b`. The gate, the
floor and the confinement are all on sibling branches.

**Confinement does not exist here, verified by execution:**

```
def _resolve_root(root):
    p = Path(root or ".").resolve()
    if not p.is_dir(): raise ValueError(...)
    return p

tmp root accepted   -> C:\Users\natew\AppData\Local\Temp\tmpjkap0syv
C:/Windows accepted -> C:\Windows
```

The brief's instruction was "verify it covers any tool you permit, by
execution, before permitting it". It covers nothing. That is why **no tool
permitted below takes a filesystem root**, and why four tools that do were
*removed* from the plan surface instead.

`permission_modes.decide()` also has **zero production callers** on this branch
(only `overview`/`describe`/`set_mode`/`MODES` are referenced). So plan mode
currently refuses nothing at the MCP surface at all; #19 is a defect in the
*classification* `decide()` reads, which is branch-independent and is what was
fixed. `b47b3ca` wires the gate at `server.py:11451` with
`decide(tool, interactive=False)` — the moment that lands, the classification
below is what it will enforce.

---

## 1. #19 — re-measured, and it is wrong in both directions

### 1.1 Method

Branch-vs-name was checked first and **does not apply at this surface**: the 184
registered MCP tools map to 184 distinct underlying functions (grouped by
`tool.fn` identity, zero alias groups), so names *are* branches here. That is
unlike the agent surface, where `_AGENT_TOOL_ALIASES` inflates a name count.
`ground_artifact`/`artifact_ground` and `artifact_verify`/`verify_artifact` look
like alias pairs and are not — different signatures, different bodies.

Risk distribution over the 184 registered tools:
`safe 63, ask 60, mutation 22, execution 26, dangerous 13`. Every registered tool
is in `command_catalog`, so nothing reaches the unknown-tool fallback. Plan
denies 121 of 184. The candidate pool is the 60 `ask` tools.

Read-only-ness was established **by execution**, not by name: each tool run
against traps on file writes, `os`/`shutil` mutation, `subprocess.run`/`Popen`,
outbound `socket.connect`, and sqlite `INSERT/UPDATE/DELETE/REPLACE/DROP/ALTER`.
Idempotent `CREATE ... IF NOT EXISTS` and creation of the runtime home were
classified as bootstrap, not writes. Positive control: `task_create` trips
`INSERT INTO tasks`; `sonder_remember_fact` trips `INSERT INTO facts` plus three
outbound calls. All probes ran against a throwaway `SONDER_HOME`; the operator's
store was opened `mode=ro&immutable=1` for counting only.

### 1.2 The count

**Filed: "~20 read-only tools refused". My re-measurement: 16 refused that
should be permitted, plus 4 permitted that should be refused. Mine governs.**

The filed figure is in the right neighbourhood for the first direction and
entirely misses the second, which is the one that matters.

### 1.3 Permitted — 16, each verified by execution

`task_list`, `task_show`, `checklist_show`, `admin_status`, `admin_whoami`,
`autopilot_status`, `calibration_status`, `learn_tiers`, `live_reload_status`,
`mcp_runtime_status`, `reasoning_show`, `sonder_sessions`, `sonder_stats`,
`turn_inspect`, `workflow_list`, `memory_export`.

All 16 scored zero semantic side effects on their **success** path, each in a
**cold interpreter**. Confinement evidence, per tool: none of the 16 takes a
`root`, `path`, `cwd`, `extra_roots`, `paths` or `url` parameter — checked
programmatically over `inspect.signature` and asserted by
`test_nothing_plan_permits_takes_a_filesystem_root_argument`. The missing
`_resolve_root` confinement is therefore **not reachable through any of them**.
None is in `grounded_outcomes.GENERATORS` or `VERIFIERS`, so none can file an
outcome row either.

### 1.4 Refused, with the reason each was refused

| Tool | Observed |
|---|---|
| `debug_inspect` | spawns `nvidia-smi` **and** `powershell -Command Get-CimInstance`, plus three calls to the model endpoint |
| `npu_status` | spawns `powershell Get-CimInstance` |
| `apply_learned` | three outbound model calls **and** `INSERT OR REPLACE INTO vectors` |
| `runtime_policy_status` | outbound call to `127.0.0.1:11434` |
| `admin_accounts` | returns `ERROR: login required.` — success path could not be exercised. Unverified is not verified-safe |
| `permission_mode` | the getter is clean; `permission_mode(mode="auto")` writes `permission_mode.json`. Allowing it under plan lets a plan run **leave plan mode** |
| `artifact_verify`, `ground_artifact` | `grounded_outcomes` VERIFIERS — a run can write an outcome row |
| `session_export` | no session fixture available; success path could not be exercised |

`npu_status` is the one worth flagging: it measured **clean** in the first sweep
and is not clean. An earlier `debug_inspect` in the same process had warmed the
hardware cache. Every verdict was re-taken cold afterwards.

`admin_accounts`, `session_export` and `artifact_verify` were initially recorded
clean while returning `login required` / `no session ''` / `not a pack
directory` — three probes passing on a refusal or not-found path. Re-run with
real fixtures (`task_create`/`checklist_create` seeded in the throwaway home)
before any verdict was kept.

### 1.5 IMPORTANT (new) — plan mode already allowed reading any directory

`diff_files`, `find_references`, `secret_scan`, `test_discover` all resolve a
caller-supplied `root` through the unconfined `_resolve_root`. All four are in
`_WORK_INSPECTION_TOOLS`, so `_risk_for` returned `safe` and **plan allowed
them**. Reproduced:

```
secret_scan      in REPOSITORY_READ_ONLY_TOOLS=False  in _WORK_INSPECTION_TOOLS=True  plan=allow

planted a fake secret at: C:\Users\natew\AppData\Local\Temp\OUTSIDE-THE-REPO-.../creds.env
repo is: D:\sonder-wt\20-standing-planmode

secret_scan(root=<outside the repo>) ->
secret scan: 1 finding(s) in 1 files scanned
  creds.env:1  [AWS credential]  AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMP...
```

A different drive, outside every project root, under the mode that promises
"reads only — no writes, no commands". `b8a15ef` removed exactly these four from
`REPOSITORY_READ_ONLY_TOOLS` for want of confinement; the removal never reached
the risk classification, so the hole stayed open on the surface where it matters
most. Now refused via `_UNCONFINED_ROOT_TOOLS`, checked *before* the read-only
sets in `_risk_for`, with a general guard
(`test_no_tool_plan_allows_reaches_the_unconfined_root_resolver`) so a fifth
cannot be added silently.

---

## 2. #23 — the standing, and whether a percentage is honest

### 2.1 What it is derived from

`calibration.measure(conn, population)` →
`memory_store.outcome_signal_counts(conn)` → `SELECT signal, COUNT(*) FROM
outcomes GROUP BY signal`, filtered to one named population. `CALLER_JUDGED` =
`used/copied/edited/accepted/rejected`; `EXECUTION_GROUNDED` =
`tests_passed/compiled/failed`. Below `MIN_SAMPLE = 20` the verdict is
`unmeasured` and no rate exists.

It was rendered to the **caller** by exactly one surface, the `calibration_status`
MCP tool. `should_verify` and `caution` — the parts the module's own docstring
calls "the load-bearing part" — had **zero production callers**. The agent
received nothing.

### 2.2 How many rows back it (real store, read-only)

`C:\Users\natew\AppData\Local\sonder\memory.db`, opened `mode=ro&immutable=1`.
**9,450 outcome rows total.**

| Population | good | bad | n | rate | verdict |
|---|---|---|---|---|---|
| **caller** | 123 | 95 | **218** | 56.4% | **poor** |
| execution | 9,049 | 183 | 9,232 | 98.0% | good |

Signal breakdown: `tests_passed 9049`, `failed 182`, `rejected 95`, `edited 60`,
`accepted 54`, `used 9`, `compiled 1`. Nothing falls outside the two
populations.

Recency, because a stale standing is a different problem from a thin one: the
caller population is **live** — 152 rows in July, **66 in August**, most recent
`2026-08-10 23:54`.

### 2.3 Is surfacing a percentage honest? Yes — for one population, and only with n

**For `caller`: yes.** n=218 is an order of magnitude above `MIN_SAMPLE`, and
the population is current. The honest figure is 56.4%, and it is *bad news* —
the reason to show it is precisely that it is not reassuring.

**For `execution`: no, and it is not shown.** 98.0% over 9,232 rows is
self-graded curriculum. It answers "did something build", not "was the delegated
work any good". It is ~42x larger and ~42 points higher, so substituting it, or
blending, produces the reassuring number this defect is about. A test asserts
the agent is never shown the two averaged, with a non-vacuity check that the two
rates differ by >0.3 so a blend would actually be detectable.

The sibling lane's inertness finding was **verified independently and is worse
than filed**, but does *not* undermine the caller figure:

- 10 of 19 `GENERATORS` never reach `_record_direct_tool` under their own name
  (agrees with `15-codegen-loop`).
- 3 of 11 `VERIFIERS` likewise (agrees).
- **New:** 2 more generators (`file_write`, `file_edit`) *are* recorded but never
  with an `output=` kwarg, so the footer is structurally impossible for them.
  Only **7 of 19** generators can even in principle reach `note_generation`.
- The `[interaction_id:]` footer is emitted by exactly three functions —
  `_answer_with_history_impl`, `_offload_impl`, `_sonder_impl_serialized` — and
  **none of them is a `GENERATORS` name**. The intersection is empty.

Reproduced end to end rather than argued from AST: after two *successful*
generator runs (`file_write`, `json_patch`, with `SONDER_FILE_ROOTS` authorised
so they did not fail for the wrong reason) and two *real* verifier runs
(`run_code`, `test_run`):

```
AFTER GENERATORS  noted=0 pending=0
  any generator output carried the footer?  False
FINAL stats={'noted': 0, 'attributed': 0, 'expired': 0, 'unlinked': 2}
FINAL outcomes table = {}
```

So the grounded feed writes zero rows. The caller population is unaffected
because those 218 rows come from `record_outcome`, the manual model-facing tool,
not from this feed. What the dead feed actually costs is the *broadening* of
both populations that plan 02 Task 1 intended — reported, not fixed here.

### 2.4 What was surfaced, and how the agent tells the three apart

`calibration.standing()` returns an explicit state; `agent_notice()` renders it
into the agent transcript beside the tool allowlist, local and hosted alike.
The distinction is structural, not prose: `should_verify` returns `(True, ...)`
for *both* a measured-poor record and an unmeasured one — same boolean, two
different facts — which is the exact collapse the claim-review lane fixed, where
"the tool ran and found nothing" was indistinguishable from "the tool was never
permitted to run".

**verified and good** (`verified-good`):
```
VERIFICATION STANDING: verified-good
  population 'caller': 90 good / 4 bad (95.7% over n=94) - good
  Self-graded execution outcomes are counted separately and are not
  included in this figure.
  Measured good, so this is 'verified and good' for past work only.
  It is not evidence about this run. Say what you checked.
```

**not verified** (`unverified`) — what the real store produces today:
```
VERIFICATION STANDING: unverified
  population 'caller': 123 good / 95 bad (56.4% over n=218) - poor
  Self-graded execution outcomes are counted separately and are not
  included in this figure.
  Measured below the 85% bar, so treat your own output as
  unverified until a check confirms it. Do not report success on
  your own say-so; cite a check, or report the work as unverified.
```

**could not be verified** (`unverifiable`) — and it quotes **no rate at all**:
```
VERIFICATION STANDING: unverifiable
  population 'caller': 3 judged outcomes on record, below the 20 needed
  to measure anything. There is no reliability figure to quote here.
  This is 'could not be verified', which is NOT 'verified and fine'.
  Cite a check you actually ran, or report the work as unverified.
```

The state word differs, the sample size is always present, and the
`unverifiable` branch is the only one with no `%` in it — asserted both ways, so
"never print a rate" cannot satisfy both tests.

**Deliberately not done:** no new gate. Plan 02 Task 2 (making `should_verify`
gate the end report) is a separate lane's task and would collide; this lane
surfaces the fact and leaves the gating seam clean.

---

## 3. Mutation proof — each planted, run, reverted

Restored from an in-memory copy, never `git stash`.

| Mutation | Result |
|---|---|
| drop the unconfined-root check from `_risk_for` | **2 failed** — `test_no_tool_plan_allows_reaches_the_unconfined_root_resolver`, `test_plan_refuses_tools_whose_root_argument_is_not_confined` |
| drop the observation set from `_risk_for` | **3 failed** — `test_plan_permits_every_verified_read_only_observation_tool`, `test_those_tools_are_classified_safe_...`, `test_manual_mode_stops_asking_about_them_too` |
| drop `task_list` from `_RUNTIME_OBSERVATION_TOOLS` | **3 failed** — same three |
| add the PowerShell-spawning `debug_inspect` to the observation set | **1 failed** — `test_plan_still_refuses_everything_that_was_observed_to_write_or_execute` |
| collapse `unverifiable` into `unverified` in `standing()` | **4 failed** — `test_all_three_states_render_differently`, `test_an_unmeasurable_standing_quotes_no_percentage`, `test_standing_never_collapses_unmeasured_into_poor`, `test_unmeasurable_and_poor_do_not_read_the_same_to_the_agent` |
| quote a rate the `unverifiable` branch cannot support | **1 failed** — `test_an_unmeasurable_standing_quotes_no_percentage` |
| stop appending the standing to the agent transcript | **9 failed** |

All reverted; `27 passed` before and after. The guard binds in every direction
tested.

Non-vacuity is asserted, not assumed: the registry floor (≥150), the
resolve-root extractor floor (≥10 functions), plan must still deny ≥80 tools and
must still allow `file_read`/`text_search`, the transcript seam must have
actually reached the model, and the three fixtures must land on three different
verdicts.

## 4. Test evidence (verbatim pytest summary lines)

RED, both new files (29 items — the **final** count), against production files
restored with `git show HEAD:...`. **`git stash` was never run.**

```
19 failed, 10 passed in 1.87s
```

Headline failures, verbatim, all `AssertionError` and all behavioural:

```
E  AssertionError: the standing is computed and shown to the caller but never to the agent
E  AssertionError: plan refuses 16 tools that were verified read-only by execution: [...]
E  AssertionError: plan mode promises reads only, but these allowed tools resolve a
   caller-supplied root with no allowed-roots check: ['diff_files', 'find_references',
   'secret_scan', 'test_discover']
```

GREEN, same two files:

```
29 passed in 1.56s
```

An earlier RED was taken at 27 items; two naming/drift guards were added
afterwards (see §5), so RED was **re-measured at the final 29** rather than
reported from the smaller run.

**Regression set**, chosen by `scripts/select_regression_tests.py`: it selected
**98 of 315 test files** from **38 changed identifiers** across `calibration`,
`command_catalog`, `server`, and reported 2 identifiers no test names
(`GOOD_AT_OR_ABOVE`, `Measurement` — both pre-existing).

```
2671 passed, 16 skipped, 4 subtests passed in 172.94s (0:02:52)
```

The full suite (~522 s) was **not** run.

No test encoded this defect as a requirement — nothing needed an assertion
changed. `tests/test_calibration.py`, `tests/test_command_catalog.py`,
`tests/test_read_only_agent_policy.py`, `tests/test_tool_capabilities.py`,
`tests/test_advertised_surface_drift.py` and
`tests/test_claim_review_hosted_vocabulary.py` were all in the selection and all
passed unchanged.

## 5. New findings

**IMPORTANT (new, fixed here).** *Plan mode allowed reading any directory on the
machine.* §1.5. Four tools classed `safe` resolve a caller-supplied root with no
confinement; `secret_scan` was driven at a directory on another drive and
printed the credential material it found. The mode's own blurb is "reads only —
no writes, no commands", and reading arbitrary filesystem locations is not what
a caller consents to by choosing the *most* restrictive mode. This is the
inverse of the filed defect and strictly the more serious half.

**IMPORTANT (new, reported, not fixed).** *The `check_error_signals.py` CI
ratchet is red on this branch and was broken by the sibling lane.* Bisected
across the seven branch commits:

```
9f377f1 -> 0    b8a15ef -> 0    277fd27 -> 0    8ad7306 -> 0
3e27ae6 -> 1    746d18b -> 1
```

`3e27ae6` (the dead-vocabulary #45 fix) added four `return "ERROR: ..."` sites in
`_agent_run_tool_policy_error` that the baseline allows zero of. Plan 02 lists
"`python scripts/check_error_signals.py` must stay silent" as a global
constraint, and the dead-vocab report does not mention running it. Output is
byte-identical with and without my change, so this is inherited, not introduced.
Left for the owning lane rather than edited across lanes.

**IMPORTANT (new, reported).** *Only 7 of 19 generators can reach
`note_generation`, not 9.* §2.3. Beyond the 10 that never record under their own
name, `file_write` and `file_edit` record without an `output=` kwarg, so the
footer cannot be present regardless of what they emit. And the footer's three
producers are `_impl` helpers, none of them a `GENERATORS` name — so the
intersection is empty and the feed is inert, reproduced by execution
(`noted=0`, `unlinked=2`, empty outcomes table).

**MINOR (new, reported).** *`scripts/select_regression_tests.py` missed
`tests/test_permission_modes.py`* — the single most relevant file for #19. The
selector keys on *changed* identifiers; `permission_modes.py` itself was not
changed, and its test file names none of the changed symbols. This is the same
class of miss the selector was built to fix, one module up: a test of a
*consumer* of the changed code. Run explicitly as a supplement, together with
`test_process_risk_server.py`, `test_permission_rules.py`, `test_slash_menu.py`:

```
162 passed in 2.36s
```

**MINOR (new, reported).** *`artifact_verify` is `ask` while `verify_artifact` is
`safe`.* Two verification readers with near-identical names on opposite sides of
the plan boundary. Not merged here — `artifact_verify` is a `grounded_outcomes`
VERIFIER and stays refused on the merits.

**Method note worth keeping.** Three separate probes in this lane passed for the
wrong reason before being caught: `admin_accounts` measured clean while
returning `login required`; `npu_status` measured clean because a previous tool
in the same process had warmed its cache; and the generator half of the feed
probe failed on file-roots confinement rather than on the footer. Cold
interpreters, real fixtures, and reading the tool's actual return value fixed
all three. A clean measurement of a path that never executed is the default
failure mode here, not the exception.

## 6. Commits

- `31185f1` — Fix what plan mode refuses, and what it should never have allowed (#19)
- `e69f370` — Tell the agent the standing its own claims are being made under (#23)
- this report committed separately

Checkout left clean on `work/20-standing-planmode`. Nothing pushed. **`git stash`
was never run; `refs/stash` untouched** — production files were restored from
`git show HEAD:<file>` and from copies parked in the session scratchpad. No
`git add -A`; staging was always by explicit path. No sibling worktree was
touched, no vendored `app/build/**/local-system/*.py` was edited, the live
benchmark was not run, and the operator's memory DB was opened
`mode=ro&immutable=1` for counting only and never written.
