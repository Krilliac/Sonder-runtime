# #51 / #47 — `/login` graded `safe`, and the grading rule behind it

Branch `work/25-login-grading`, from `3e3ae60`. Commit `92e177c`.
Every `file:line` in the brief was re-resolved; three were wrong (below).

---

## 0. Lineage, checked rather than trusted

```
git merge-base --is-ancestor 9f377f1 HEAD  -> 0   (9f377f1 IS an ancestor)
HEAD = 3e3ae603a9cfc157acb966e0823529f28806c6b3  on work/25-login-grading
```

The three claimed prior fixes are present and were confirmed by import, not by grep:

| claimed | present | evidence |
|---|---|---|
| `command_catalog.reset_cache()` | yes | `command_catalog.py:935` (def), called `reloadable_mcp.py:275` |
| non-degradable `UNCLASSIFIED` | yes | `permission_modes.py:271`, in all four rows of `_MATRIX` |
| `/location` gating | yes | `_UNREGISTERED_BRANCH_WORK["/location"] = "location"`; `risk_of("location") == "ask"` |

### Anchors in the brief that do not resolve

* **`command_catalog.py:478-481`** — not the grading rule. The real rule was
  **`command_catalog.py:884`**. (`:873` is not a vendored copy either.)
* **`sonder_repl.py:1068`** — not `CURRENT_TOKEN`. The write is **`sonder_repl.py:1283`**.
* **`server.py:7520`** — `_file_developer_allowed` is at **`server.py:7675`**.
* **"seven guarded call sites"** — measured **13**
  (`grep -c "developer_authorized=_file_developer_allowed(token)" server.py`).
* **`app/build/**/local-system/*.py`** — **no such tree exists in this worktree.**
  `app/` here is a Flutter client (`lib/`, `pubspec.yaml`); there is exactly one
  `command_catalog.py` in the repo. The vendored-copy hazard is real on some
  lineage, but not this one, so no anchor here can have pointed into it.

---

## 1. #51 — reproduced before fixing

Isolated fixture home (`SONDER_HOME` repointed at a scratch dir), fixture
account created locally, **no network and no real credentials**. The `/login`
branch was replayed exactly as `sonder_repl.py:1275-1284` performs it.

```
workspace_root: D:\sonder-wt\25-login-grading
probe is protected-mutation path: True

=== BEFORE /login (CURRENT_TOKEN == '') ===
ERROR: refusing to mutate protected Sonder control-plane path without an
authenticated developer token: D:\...\__repro51_probe__.py
file exists after attempt: False

=== /login (exactly as sonder_repl.py does it) ===
token acquired: True len 48
_file_developer_allowed('')    = False
_file_developer_allowed(tok)   = True

=== AFTER /login (same call, token threaded) ===
file write
  path: D:\...\__repro51_probe__.py
  bytes: 8
  action: create
file exists after attempt: True

=== grading of /login ===
name=/login tool='' risk=safe
```

A guarded-root write **refused before `/login`, succeeded after it**, with
nothing else changed — and the command that flipped it was graded `safe`.

---

## 2. #47 — re-measured. My figures differ from the brief; mine govern.

Measured by importing the real catalog, not by pattern match:

| figure | brief | **measured** |
|---|---|---|
| catalog entries | 271 | **273** |
| tool-less entries | 87 | **88** |
| tool-less graded `safe` | (48 "write durable state") | **51** |
| tool-less graded `ask` / `dangerous` / `mutation` | — | 31 / 5 / 1 |

**The brief's "48 write durable state" is not reproducible as stated**, and I did
not reproduce it, because "writes durable state" is not a property the code
exposes — it is a judgement someone made per command. I replaced it with a
property that *is* measurable and is the one that matters:

> **30 native commands are graded below the risk of the tools their branch
> actually calls. 5 of those reach a tool named in `_DANGEROUS`:**
> `/qualityfix`, `/todo`, `/register`, `/setaccount`, `/runtime`.

Two of the brief's specific claims are **wrong**: `/mcp` is graded `dangerous`,
not `safe` (it has an `_UNREGISTERED_BRANCH_WORK` stand-in), and `/model` and
`/new` front no tool the derivation can see, so they are declared-`safe` rather
than mis-derived. `/contextsize`, `/strict`, `/resume`, `/project` are likewise
not all under-graded; the measured list above is the accurate one.

### Why `/setaccount` and `/register` are downgraded despite fronting `_DANGEROUS` tools

Not a second bug in the danger marking — the marking is never consulted.
`catalog()` resolved the fronted tool by **string identity**:

```python
tool = next((n.lstrip("/") for n in group if n.lstrip("/") in tools_by_name), "")
```

`/setaccount` → `"setaccount"`, but the tool is `admin_set_account`.
`/register` → `"register"`, but the tool is `admin_register`. Neither matches,
so `tool == ""`, and the command fell into the *tool-less* default — the very
rule in #47. **The catalog never learns the command fronts a dangerous tool, so
there is nothing to override.** The name-matching heuristic silently swallows an
explicit `_DANGEROUS` marking.

The sharp consequence, measured:

```
risk_of("admin_set_account") = dangerous
risk_of("setaccount")        = safe
```

**The same command grades differently depending on which of its two names you
hand the classifier.** And `console_tools()` — the derivation the *permission
gate* reads — resolved both correctly all along. So the gate refused what the
help surface called safe: two surfaces, two answers, one command.

---

## 3. The grading approach, and why not the alternatives

**Not "grade tool-less commands stricter."** That is the brief's own trap: it
would worsen `/delete` and sweep ~39 genuinely clean commands into friction.

**Not a curated table.** A hand-maintained security table going stale silently
is *exactly* how `/setaccount` came to be graded safe. `_branch_tool_calls`'s own
docstring already says so.

**Chosen: derive it from the branch, reusing the derivation that already
exists.** `console_tools()` walks each dispatch branch's AST and resolves the
tools it really calls. `_native_risk()` now takes the **strongest** of:

1. the declared value (`command_registry`) — so a human judgement about a command
   fronting no tool at all (`/mcp`, `/selfmod`) still stands;
2. the name-matched tool, unchanged, where it does work;
3. every registered tool the branch actually calls.

Stand-in names from `_UNREGISTERED_BRANCH_WORK` (`mcp`, `selfmod`, `location`,
`hardware`, `training`) are deliberately excluded from (3). They are not
registered tools, and grading them via `permission_modes.risk_of` would **recurse
straight back into this catalog** — `risk_of("mcp")` → `by_name("/mcp")` →
`catalog()`. Four existing tests depend on those keeping the declared path.

**How it stays correct at command 272:** it is a derived property, so a new
branch is graded the moment it is written. Proven, not asserted — see M5 below.

### The inverse error is fixed too

`/delete` was graded `dangerous` for a delete it cannot perform: the branch is
`server.file_delete(path=..., dry_run=True, token=...)` with the literal written
in. `_DISARMING_ARGUMENTS = {"file_delete": {"dry_run": True}}` de-escalates it.
Two properties stop this being a bypass:

* **Evidence-based** — the literal must be visible in the AST at the call site.
  A variable, an expression, or a caller-supplied value does not match, so
  nothing that could be `False` at runtime is ever de-escalated.
* **Attached to a call site, never to a tool** — `/file_delete`, the direct MCP
  spelling that takes `dry_run` from its caller, stays `dangerous`.

Result: `/delete` `dangerous` → `safe`; `/file_delete` unchanged at `dangerous`.

---

## 4. Mutation results — every guard planted, observed, reverted

A guard green on arrival proves nothing, so each was broken deliberately.

| # | mutation | result |
|---|---|---|
| M1 | drop `admin_login` from `_DANGEROUS` | **caught** — 1 failed, 12 passed |
| M2 | revert derivation (`return declared`) | **caught** — 8 failed, 5 passed |
| M3 | `/delete` passes `dry_run=_dr` (variable, not literal) | **caught** — 2 failed; `/delete` re-arms to `dangerous` |
| M4 | `/delete` pins `dry_run=False` (literal, wrong value) | **caught** — 2 failed |
| M5 | plant a new branch `/zzprobe272` calling `sqlite_mutate` | graded `dangerous`, `tool=''`, catalog 273→274, **no table edit** |
| M6 | M5 **plus** reverted derivation | **caught by name**: `('/zzprobe272', 'safe', ['sqlite_mutate'])` |

M5 alone passing would have been ambiguous (fix working vs. test blind), so M6
pairs it: the same planted command is caught the instant the derivation is
removed. That is what makes "correct at command 272" a measurement.

---

## 5. TDD

**RED** — final item count (13), parent source restored via `git show HEAD:…`
(which, unlike `git checkout <sha> -- <path>`, does not write the index):

```
======================== 11 failed, 2 passed in 1.78s =========================
```

All 11 fail with `AssertionError`, none with `AttributeError`. An earlier draft
had 3 failing on a missing attribute; `_disarmed()` now resolves via `getattr`
so those tests fail on the grade they actually assert about. Sample messages:

```
E  AssertionError: assert 'safe' == 'dangerous'          (/setaccount, /register)
E  AssertionError: assert 'ask' == 'dangerous'           (risk_of("admin_login"))
E  AssertionError: assert 'dangerous' != 'dangerous'     (/delete)
E  AssertionError: commands reaching a dangerous tool but not graded dangerous:
     [('/qualityfix','ask',...), ('/todo','safe',...), ('/register','safe',...),
      ('/setaccount','safe',...), ('/runtime','ask',...)]
```

The 2 green at RED are intentional: the `/login` escalation characterization
(it describes the mechanism, which the fix does not change) and the
`execution`-vocabulary pin (a pre-existing gap, recorded so it cannot widen).

**GREEN** — the new file, then every one of the 19 suites that import
`command_catalog`, `command_registry`, or `permission_modes`:

```
tests\test_command_grading.py .............                              [100%]
============================= 13 passed in 1.98s ==============================

498 passed in 24.64s        (12 suites: grading, registry, router_catalog,
                             permission_modes, serve_commands, gate_coverage,
                             gate_dispatch, risk_of_fail_closed, catalog,
                             serve_history, reloadable_mcp, router)
236 passed in 7.34s         (8 suites: agent_dispatch_dev_tools, autopilot_server,
                             claude_like_controls, gate_http, policy_display,
                             repl_catalog, slash_menu, sonder_migration)
```

The full suite (~522s) was not run, per the brief.
`scripts/select_regression_tests.py` is **absent on this lineage**, so suite
selection was done by grepping for importers of the three changed symbols.

### One test broke, and it was read before it was touched

`tests/test_command_registry.py:6` — `assert "/delete" in format_commands("dangerous")`.
`format_commands` filters **catalog** rows, so this asserted `/delete` is
dangerous — the inverse error itself. It is **not** a test encoding the defect
as a requirement: its purpose is "the risk filter works", and `/delete` was
merely a convenient witness. The witness was changed to `/setaccount` (genuinely
dangerous, and correct only *because* of this fix), plus an explicit
`"/delete" not in dangerous`, with the reasoning in-line.

---

## 6. NEW findings

### IMPORTANT — grading `admin_login` `dangerous` does **not** stop it off-console

Measured after the fix:

```
mode         risk       action (interactive=False)
plan         dangerous  deny   allowed=False
manual       dangerous  allow  allowed=True
acceptEdits  dangerous  allow  allowed=True
auto         dangerous  allow  allowed=True
```

`dangerous` is ASK in three of four modes and `decide(interactive=False)`
degrades ASK to ALLOW. This is the residue of the #18 finding: that lane made
`UNCLASSIFIED` non-degradable but left `dangerous` degradable. **For an
elevation primitive, "ask a human" collapsing to "allow" when no human is
present is precisely inverted.** What the fix does buy, measured: interactive
`auto`/`acceptEdits` go `allow` → `ask`, and `/login` enters
`command_router._RISKY`, so it can no longer be auto-resolved from a partial or
fuzzy natural-language match. Do not read the `dangerous` label as "blocked
everywhere" — it is not, and that gap is not mine to close here.

### IMPORTANT — the catalog cannot express `execution`

`permission_modes.risk_of` layers on a synthetic `execution` class from
`EXECUTION_TOOLS`; `command_catalog._risk_for` has no such branch, so `/run`,
`/runscript`, `/forge` and `/train` store `ask`. Measured **before and after**
this change — unchanged, so pre-existing, not a regression. It does not reach
enforcement (the gate grades the *tools*, and `_RISKY` contains neither class),
so it is display/classification drift. Pinned by
`test_execution_class_is_absent_from_the_catalog` so it cannot widen silently.
Note `tests/test_serve_commands.py:53` quietly assumes `execution` never appears
in a catalog row.

### Note — a deliberate divergence

`command_registry.COMMANDS` (the legacy seed) still declares `/delete` as
`dangerous` in its own hand-written row. The catalog now overrides it. Left as
is: the seed is documentation of intent, and rewriting it is outside this task.

---

## 7. Constraints observed

* `git stash` never invoked. `stash@{0}` / `stash@{1}` verified present and
  untouched (listed only).
* Index checked empty before committing (`git diff --cached --stat`); parent
  source restored with `git show HEAD:… >`, never `git checkout <sha> -- <path>`.
* No `git add -A`; three paths staged explicitly. Sibling worktrees untouched.
* `sonder_repl.py` restored byte-identical after M3/M4/M5 — it is **not** in the
  commit; the whole fix is in the grading layer.
* No real `/login`, no network, no real account. No memory DB, no benchmark.
* Not pushed. Checkout clean.
