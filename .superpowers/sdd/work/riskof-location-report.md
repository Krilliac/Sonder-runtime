# Tasks #18 / #37 — `risk_of` fails open, and `/location`'s false allow-list reason

Branch `work/18-riskof-location`, from `5b3ae1a`. Companion to
`merge-resolution.md` and `fix-critical.md`, which established the gate and the
floor this builds on. Every `file:line` was re-resolved against the tree.

---

## 1. #18 — what `risk_of` returns when it cannot classify

### The filed claim is right, and it is wider than filed

`risk_of` (`permission_modes.py:479`) had **three** ignorance branches, and all
three resolved to a grade that `decide(interactive=False)` turns into `allow`:

| branch | returned | what the code claimed | what happened non-interactively |
|---|---|---|---|
| empty name (`:490`) | `"ask"` | *"Fail closed"* | **allow** in every mode but `plan` |
| `CatalogUnavailable` (`:503`) | `"dangerous"` | *"`dangerous` is the one class that stops in every mode"* | **allow** in manual/acceptEdits/auto |
| unknown name (`:519`) | `"ask"` | *"never as safe"* | **allow** in every mode but `plan` |

The gate then does nothing with the grade, because the degrade happens *after*
it:

```
decide('no_such_tool_xyz', interactive=False)
  -> action=allow allowed=True risk=ask
     reason='no interactive prompt available; ask tools are not blocked outside the console'
     mode=manual allow | auto allow | acceptEdits allow | plan deny
```

### The load-bearing correction: severity cannot fail closed here

The second row is the one that matters, and it was measured rather than
reasoned:

```
=== the CatalogUnavailable fail-closed path, end to end ===
  risk_of(git_merge) with blind catalog = 'dangerous'
  mode=manual      risk=dangerous action=allow  allowed=True
  mode=acceptEdits risk=dangerous action=allow  allowed=True
  mode=auto        risk=dangerous action=allow  allowed=True
  mode=plan        risk=dangerous action=deny   allowed=False
```

`dangerous` is `ask` in three of four modes (`_MATRIX`), and `interactive=False`
degrades `ask` to `allow`. **Every** production gate passes `interactive=False`
— `reloadable_mcp._refuse_if_gated`, `server._agent_permission_gate_error:14505`,
`server._loop_permission_refusal:11518`, `server._control_tool_refusal:2140`,
`sonder_serve._http_tool_refusal:958`. So on the only surface that enforces, **no
severity of grade fails closed**. The previous attempt at failing closed —
returning a scarier class — was inert, and its comment asserted the opposite.

This is Shape A inside the guard that was itself written to fix Shape A.

### MEASURED count of tools on the fallback path: **1**

Not estimated. A census of every name each gate can actually hand `risk_of`,
taking the real argument of each `decide()` call:

```
reloadable_mcp (every MCP tool)        names=185  fallback=0
_agent_dispatch (canonicalized)        names=131  fallback=0
console_tools() map values             names=89   fallback=0
http_slash_tools() map values          names=79   fallback=0
_loop_dispatch (mapped)                names=68   fallback=1  ['sleep']

DISTINCT fallback names across all bounded surfaces: 1
```

**A count of 1 is the classic tell, so here is what produced it.** Two earlier
numbers were wrong and were corrected before being reported:

* **v1 said 0 for `_loop_dispatch`.** My AST walk whitelisted `tool_name`/`cmd`;
  `_loop_dispatch` compares `action_type`. That zero was an early abort in the
  *measurement*, not a clean surface. Fixed → 85 names, 1 hit.
* **v1 also flagged `/council` on the http chain.** Wrong: the slash gates
  resolve `cmd` → tools through `command_catalog.{console,http_slash}_tools()`
  and grade the *tools*; slash names never reach `risk_of`. Over-count removed.
* Vacuity control: the detector fires on a planted name
  (`is_fallback('definitely_not_a_tool_zzz') = True`) and does not fire on a
  graded one (`is_fallback('file_delete') = False`).

`sleep` is a clamped `time.sleep` in `_loop_dispatch` that runs no tool, and
`tests/test_permission_gate_dispatch.py` already blessed it in writing
("Actions that run no tool at all (`sleep`) are fine to leave unresolved").

**So the defect is real but not firing on any dangerous name** — the third time
this fleet has produced that result. What is *not* bounded: `_loop_action_tool`
is `_LOOP_ACTION_TOOLS.get(name, name)`, so an arbitrary model-authored `type`
string reaches `risk_of` verbatim (`_loop_action_tool('rm -rf /; anything')`
returns it unchanged). Unknown types fall through to "unknown action type" and
execute nothing, so it is not exploitable today; it is the reason the fix is
worth making rather than waiving.

### The 28-tool `ask` → `allow` case is genuinely distinct — with a correction

The sibling lane's ruling **holds**, and the two are not the same thing. Full
reconciliation of the 28:

```
raw branch names      : 140
canonicalized (gated) : 131
RAW    risk_of==ask: 28   (catalogued 21 / uncatalogued 7)
       uncatalogued: agent_cancel, agent_capacity, agent_retry, agent_status,
                     game_campaign, game_generate, improvement_report
CANON  risk_of==ask: 19   (catalogued 19 / uncatalogued 0)
```

The deliberate cohort and the fail-open are **disjoint** (overlap 0), so they
were never conflated in effect. But the *figure* was: **28 is the count over raw
names, and 7 of the 28 are alias names the catalog has never heard of** — i.e.
they were the fallback, the very thing the sentence was contrasting against.
They are harmless only because `_agent_permission_gate_error` canonicalizes
before grading. Over the set the gate really grades: 19 catalogued `ask`, 0
fallback. The docstring carrying "twenty-eight" is corrected in place.

### The fix, and what failing closed breaks

A new grade `UNCLASSIFIED = "unclassified"` returned by all three ignorance
branches, with its own `_MATRIX` row (DENY under `plan`, ASK elsewhere) that
`decide()` **refuses to degrade** for a non-interactive caller. Severity could
not express this; the degrade had to be the thing that changed.

The anti-DoS half is deliberate and tested:

* a person at a console is **asked**, not refused (`interactive=True` → ASK);
* an explicit `allow` rule still resolves *above* the refusal, so an operator
  has a written way to permit a name the catalog cannot see;
* `describe()` gained the row and `ASK_CAVEAT` — the single source of the "ask
  means a prompt" sentence — now names the exception, since it previously stated
  the opposite for this row.

**What it actually broke, measured across 1048 tests:**

1. **`sleep`** would have been refused — the classifier denying service to a
   `time.sleep`. Declared in `permission_modes.NON_TOOL_WORK` (grade `safe`),
   the way `EXECUTION_COMMANDS` declares `/runwindow`, with a drift test pinning
   it to loop actions that really run no tool and really front no registered
   tool.
2. **Live-reload hot-add** (`test_reloadable_mcp.py`, 2 tests). This exposed a
   **real bug, independent of this change** — see §3.
3. Nothing else. The 71 catalogued-`ask` MCP tools and the 19 dispatchable ones
   are untouched; the single cohort member that refuses
   (`admin_private_chain_of_thought`) does so at `source=rule, risk=ask` from a
   shipped deny rule, pre-existing.

### Mutation — the guard binds at the gates, not just in `decide()`

An unclassifiable probe planted at each production gate:

```
[1] server._agent_permission_gate_error  -> ERROR: HOST POLICY: tool '...' is refused by the
                                            active permission gate (nothing could classify ...)
[2] server._loop_permission_refusal      -> refused by the permission gate (mode=manual)
[3] reloadable_mcp._refuse_if_gated      -> ToolError: ... is refused by the active permission gate

[4] CONTROL - real graded tools are NOT refused
    status     risk=safe  agent=allowed loop=allowed direct=allowed
    file_read  risk=safe  agent=allowed loop=allowed direct=allowed
    sleep      risk=safe  agent=allowed loop=allowed direct=allowed
[5] CONTROL - deliberate catalogued-'ask' cohort: size=58, refused by this change=0
```

### Tests that encoded the defect as the requirement — 2 found, both rewritten

* `test_permission_modes.py::test_unknown_tool_is_never_treated_as_safe` —
  titled "Fail closed", asserted `risk_of(name) == "ask"`, and checked **only**
  `decide(..., interactive=True)`. The assertion was true and the property in
  its own name was false. Now asserts the non-interactive refusal it always
  claimed.
* `test_permission_gate_dispatch.py::test_a_dangerous_tool_does_not_downgrade_when_the_catalog_is_blind`
  — its docstring explicitly waived the failing half: *"a caller with nobody to
  ask still gets `allow` here ... that degrade is the 'preserve current
  behaviour' contract and is unchanged"*. Every production gate is such a
  caller, so the disclaimer covered the entire enforced surface. Rewritten to
  assert the manual/acceptEdits/auto refusals too.

Neither assertion was re-baselined without reading it.

---

## 2. #37 — `/location`'s allow-list reason

### Anchors re-verified programmatically — both correct, no drift this time

```
sonder_repl.py:781   location_consent = None   # None = env default
sonder_repl.py:1080  elif cmd == "/location":
sonder_repl.py:1083  location_consent = a == "on"          <- THE WRITE
sonder_repl.py:1510  location_consent=location_consent)    <- forwarded to server.sonder
```

`/location` exists **only** in `sonder_repl.main` — not in `control_command`, not
in `sonder_serve._handle_slash`, not in `_agent_dispatch`.

### Does the write reach later turns? **Yes — proven by execution**

Driven through the real `sonder_repl.main()` with `server.sonder` stubbed to a
recorder, so no geolocation was granted to real code and no network call was
made (`approximate_location_lookup` was replaced with a raising guard):

```
turn 'turn one'     -> location_consent=None      (before)
turn 'turn two'     -> location_consent=True      (after /location on)
turn 'turn three'   -> location_consent=True
turn 'turn four'    -> location_consent=False     (after /location off)

env default (untouched): _env_location_consent() = False
```

Consent is off by default and the write grants it for the rest of the session.

### Decision: **moved out of the allow-list**, not reworded

The reason was false — the branch reads the env flag only in its display path
and *writes* on `on|off`. But rewording it does not resolve the entry: by the
list's own bar — *"Anything that runs a program, writes a file, **changes what a
later call may do**, or spends the machine is NOT display only"* — a truthful
reason ("writes session consent") would state precisely the disqualifying
property. Keeping it would leave the list holding an entry that contradicts its
own admission criteria. So writing consent disqualifies it, and it is removed.

Checked the neighbours rather than assuming, as instructed: `/project` ("sets
the session's project scope; assignment only") also reaches later calls, but its
recorded reason is an accurate description of the branch body and it selects
*scope* among already-permitted paths rather than granting a capability that is
off. `/report` and `/route` are clean. `/location` remains the only false one.

### Removing it was load-bearing, not cosmetic

RED, with the entry removed and nothing else changed:

```
E       AssertionError: 1 dispatch branch(es) resolve to no tool, so the gate is consulted,
E       receives an empty set, and allows them:
E           /location (sonder_repl.py, console)
1 failed, 7 passed in 3.20s
```

And end-to-end, **before** the gate wiring — note `plan` reports `deny` for the
name while the command still runs, because the verdict never reaches the branch:

```
  mode=auto     decide=allow   later turn received location_consent=[True]
  mode=plan     decide=deny    later turn received location_consent=[True]   <-- plan bypassed
  mode=manual   decide=ask     later turn received location_consent=[True]
```

**After:**

```
  mode=auto     decide=allow   later turn received location_consent=[True]
refused /location: plan forbids ask tools (mode: plan)
  mode=plan     decide=deny    later turn received location_consent=[None]
skipped /location
  mode=manual   decide=ask     later turn received location_consent=[None]
```

A mode advertising "reads only — no writes, no commands" was letting a session
acquire a new capability.

### How it is gated

Exactly the way `/hardware` is, and for the reason recorded beside it in
`command_catalog.py`: *"a recorded `safe` verdict and an exemption behave
identically today and differ entirely tomorrow — the verdict is visible to
`/permissions`, can be overridden by a rule, and stays inside the map this repo
now has a floor for."* Moving `/location` out of the allow-list is that same
precedent applied, not a novel judgement.

* `command_catalog._UNREGISTERED_BRANCH_WORK["/location"] = "location"` — so the
  console gate resolves it to a graded name instead of `()`.
* A curated `command_registry` entry, risk `ask`. **This is required**: without
  it `catalog()` defaults a console command fronting no tool to `safe`
  (`command_catalog.py:873`, *"A console command that drives no tool cannot
  mutate on its own"*), and `plan` allows `safe`. That default is the same
  optimistic-guard shape as #18, one layer over.
* `ask` rather than `mutation` because it writes no file; the two are the same
  row of the mode matrix, so the grade is descriptive, not behavioural.

`/location off` is gated identically, which is the one wart: revoking consent is
also `ask`. Left as-is rather than special-cased — a grade that depends on the
argument is what let `/selfmod` be graded by its most harmless sibling.

---

## 3. NEW findings

### IMPORTANT — the command catalog survived every live-reload swap

`command_catalog.reset_cache()` existed, its docstring read *"used after a live
reload adds tools"*, and it had **no callers anywhere** in production code. The
catalog is an `lru_cache` over the tool registry that `reloadable_mcp` hot-swaps,
and the swap block cleared the low-level schema cache but not this one.

The catalog is the permission gate's only source of truth for a risk class, so
the consequence outlived the reload: **a reload that reclassified a tool
`safe` → `dangerous` left the gate enforcing the stale grade for the life of the
process**, and a newly added tool was unknown to the catalog entirely. Present at
the parent commit; surfaced because failing closed turned a silent mis-grade into
a visible refusal.

Fixed in the swap block. Mutation-verified — with the call removed:

```
E   AssertionError: the registry was swapped and the memoised catalog was never invalidated,
E   so the permission gate keeps grading tools from the pre-reload registry
E   assert []
```

The regression test observes the invalidation rather than the cache's end state,
because the gate re-warms the catalog on the same `call_tool` — asserting
`currsize == 0` afterwards would have been a test that can only fail.

### MINOR — `catalog()` defaults tool-less console commands to `safe`

`command_catalog.py:873`: *"A console command that drives no tool cannot mutate
on its own."* `/location` disproved it. This is the same optimistic default as
#18 and is the reason `/location` needed a curated grade rather than just a map
entry. Not swept for other instances — out of scope here, but it is a live lead
for a defect-sweep pass: any console branch fronting no tool currently inherits
`safe`, which `plan` allows.

---

## 4. Test results

Full suite (~522s) and the live benchmark deliberately not run.

**RED — `tests/test_risk_of_fail_closed.py` at the parent, final item count:**

```
15 failed, 7 passed in 2.13s
```

Representative behavioural failures (all `AssertionError`, no import/attribute
errors — the new grade is referenced as a literal in the test file so the
assertions fail on behaviour):

```
E   AssertionError: not_a_real_tool in mode manual: an unclassifiable tool resolved to 'allow'
E   with nobody to ask, so the gate downgraded it instead of refusing it
E   assert 'allow' == 'deny'
E   AssertionError: mode manual: the classifier was blind and the gate still allowed git_merge ('allow')
E   AssertionError: the unknown-tool fallback is indistinguishable from a catalogued ask
E   AssertionError: sleep grades 'ask'; it runs no tool, so it must be declared rather than
E   fall through the unknown-tool hole
```

The 7 that passed are the anti-DoS controls, which must be green before *and*
after (no registered tool may become unclassifiable).

**RED — #37, `tests/test_permission_gate_coverage.py` with the entry removed:**

```
1 failed, 7 passed in 3.20s
```

**GREEN — every file run, verbatim:**

`test_risk_of_fail_closed.py`, `test_permission_gate_coverage.py`,
`test_permission_gate_dispatch.py`, `test_permission_gate_http.py`,
`test_permission_modes.py`, `test_permission_policy_display.py`,
`test_permission_rules.py`, `test_reloadable_mcp.py`, `test_command_catalog.py`,
`test_command_registry.py`, `test_command_router_catalog.py`,
`test_repl_catalog.py`, `test_serve_history.py`, `test_serve_commands.py`,
`test_agent_dispatch_dev_tools.py`, `test_agent_verification_gate.py`,
`test_agent_tools.py`, `test_tool_capabilities.py`, `test_autopilot_server.py`,
`test_workbench_server.py`, `test_server_source_invariants.py`,
`test_server_helpers.py`, `test_read_only_agent_policy.py`,
`test_claude_like_controls.py`:

```
1048 passed in 48.92s
```

Additionally run for blast radius (web/location and git/display consumers):
`test_chat_web_routing.py`, `test_web_intents.py`, `test_web_tools.py`,
`test_web_tools_security.py`, `test_web_volatile_context.py`,
`test_serve_auth.py`, `test_local_service_probe.py` → `461 passed in 51.89s`;
`test_git_history.py`, `test_git_tools.py` (within `289 passed in 40.81s`).

---

## 5. Commits

* `fa968d4` — Fail closed when `risk_of` cannot classify a tool (task #18)
* `b40164f` — Gate `/location` instead of excusing it as display-only (task #37)

---

## Provenance

Produced 2026-08-11 in worktree `D:\sonder-wt\18-riskof-location` on branch
`work/18-riskof-location`. **No `git stash` was run and the stash refs were not
touched** — `stash@{0}` and `stash@{1}` verified present and unchanged after the
work. No `git add -A` was run; every commit staged explicit paths. No sibling
worktree was modified and nothing was pushed. The full test suite and the live
benchmark were not run. `app/build/**/local-system/*.py` were not touched.

Mutations were applied to `reloadable_mcp.py`, `command_catalog.py` and
`command_registry.py` and reverted from byte-exact copies, each verified with
`diff` reporting no difference before committing. The operator's memory DB and
stored facts were not touched.

**No real IP-geolocation was granted or triggered and no network call was made.**
Every `/location` probe stubbed `server.sonder` to a recorder and replaced
`server.approximate_location_lookup` with a guard that raises if reached; the
subprocess reload test was pointed at a throwaway `SONDER_HOME` under the session
scratchpad. All probe scripts live in the session scratchpad, never inside the
repository.
