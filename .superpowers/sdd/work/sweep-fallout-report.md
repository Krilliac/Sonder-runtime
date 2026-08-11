# Sweep fallout: fixing the defects in last night's fixes

Branch `work/27-sweep-fallout`, base `3e3ae60`. Four commits, clean checkout,
nothing pushed. No `git stash`, no `git add -A`, no sibling worktree touched.

## Lineage — verified, and the brief was wrong twice

`git merge-base --is-ancestor <b> HEAD`:

| branch | ancestor of HEAD? |
|---|---|
| `feat/verified-fetch-modes-calibration` (`9f377f1`) | **yes** |
| `work/18-riskof-location` (`3e3ae60`) | **yes** — HEAD descends from it |
| `main` (`f018265`) | yes |
| `work/13-drift-family` (`c418c3e`) | **no** |

The three fixes the brief said this lane should carry are all present and were
each confirmed by grep + execution, not by trusting the brief:

- non-degradable `UNCLASSIFIED` — `permission_modes.py:271`, wired into all four
  `_MATRIX` rows (293-301) and into `decide()`'s no-degrade branch (737).
- `/location` gating — `b40164f`.
- `command_catalog.reset_cache()` — real production caller at
  `reloadable_mcp.py:275`.

**Brief error 1 (S4).** Its reproduction — "a blind classifier reached via any
other exception yields `git_merge` -> `allow`" — does **not** reproduce here.
`fa968d4` already moved the unknown-name fallthrough to `UNCLASSIFIED`, so
`git_merge` grades `unclassified` on every blind path. That reproduction was
valid against `sdd/01-permission-gate`, the sweep's subject. A real residual
hole of the same shape survives, and is narrower — see S4.

**Brief error 2 (S5).** "Two fixes live on `work/13-drift-family` only." Only
`278839e` does. `dfad7ce` is on **`work/16-dead-vocab`**, which is what the
sweep document itself says. Both are absent from HEAD.

## S4 — a blind classifier is only blind the one way it was named

Reproduced by execution on an isolated harness before any edit, non-interactive
(the mode all five production gates use):

```
baseline (catalog healthy):
   risk_of(self_heal_repair) = dangerous

classifier blind via catalog_unavailable:
   risk_of(self_heal_repair) = unclassified  plan=deny manual=deny acceptEdits=deny auto=deny

classifier blind via other_exception:
   risk_of(self_heal_repair) = execution     plan=deny manual=allow acceptEdits=allow auto=allow  <-- ALLOWED
   risk_of(run_code)         = execution     plan=deny manual=allow acceptEdits=allow auto=allow  <-- ALLOWED
```

The `except CatalogUnavailable -> UNCLASSIFIED` guard sat beside a broad
`except Exception: command = None`, and a second `except Exception:
command_catalog = None` on the import. Both fell through to the **static tables
at the bottom of the function**, so the guard was bypassed by every way of going
blind except the one it named. `catalog()` converts only the registry read into
`CatalogUnavailable`; the ~100 lines around it are unwrapped, so the
partially-initialised server that `CatalogUnavailable`'s own docstring cites
arrives as `ImportError`/`AttributeError` — the bypassed path.

The fallthrough is not merely "ungraded", it is **softer than the catalog**.
`self_heal_repair` is catalog-`dangerous` **and** in `EXECUTION_TOOLS`, and
`dangerous` outranks `execution` only when the catalog can be read. Blind, the
dangerous test cannot fire, `EXECUTION_TOOLS` does, and `execution` is `ALLOW`
in `auto` outright. That is precisely the precedence defect `risk_of`'s own
docstring says the ordering was rewritten to fix, re-entered through the
exception path. The lesson the brief names holds and was applied: the answer is
`UNCLASSIFIED`, not a scarier class — both handlers now return it, because the
gate cannot know *why* it went blind, only that it did.

**Does the shape exist elsewhere?** AST sweep of all five production gate call
sites — `reloadable_mcp._refuse_if_gated`, `server._control_tool_refusal`,
`server._loop_permission_refusal`, `server._agent_permission_gate_error`,
`sonder_serve._http_tool_refusal`: **none has any `except` handler at all.**
The shadowing was confined to `risk_of`, where it existed **twice**, and both
are fixed.

## S1 — confinement had no write-path coverage, and no write path

Reproduced live, one project directory, both arms:

```
read_only=True    test_discover -> "test discovery: pytest"
read_only=False   test_discover -> ERROR: root is outside every authorized root: <project>
                  build_run     -> same
                  lint_run      -> same
```

`repository_extra_roots` is the only channel that adds the host-selected root to
the authorized set (it opens `harness_tools.authorized_root_scope` inside
`_agent_dispatch`), and it was passed on the `read_only` arm only. Now passed on
both. An AST sweep confirms its **only** use outside the `if read_only:` block is
`authorized_root_scope()` itself, so this grants the scope and nothing else.
Post-fix: `test_discover`, `build_run` and `lint_run` all reach the tool.

**New coverage.** Every write-path test in `test_agent_dispatch_dev_tools.py`
monkeypatches the tool away, so `_resolve_root` is never reached; the sole
end-to-end control used `read_only=True`. The new tests in
`test_harness_root_confinement.py` **monkeypatch nothing**, and come as a pair so
"pass the scope unconditionally" cannot satisfy them: the bound project must be
reachable, a sibling outside it must still be refused.

**Proof it binds:** written against the unfixed dispatcher — RED
`4 failed, 40 passed`, all four write-path params.

## S2 + S3 — the contradiction, resolved in one direction

S3 first, because it decides S2. `harness_tools` appends `path` to the child
argv with no check. Measured, the exposure is **worse than the documented
write** — with pytest as the framework it is arbitrary **code execution**
outside the authorized root:

```
SONDER_FILE_ROOTS=<proj>
test_run(root=<proj>, path="../OUTSIDE/test_evil.py")
  -> ok=True, "1 passed"
  -> the collected test body ran and wrote a marker OUTSIDE the authorized root
```

Post-fix: `ValueError: path is outside the root it was given`, marker not
written.

So `path` is load-bearing, which **refutes** `_agent_verification_covers`'s
justification for ignoring it ("narrows which checks run, not what those checks
exercise"). The contradiction resolves in one direction rather than by picking a
side: **confine `path` so it can only narrow within `root` (S3), then read it so
a narrowed run is judged on what it actually looked at (S2).** The half the old
comment had right is kept and is why the narrowing is conditional — `path` is
empty on a default invocation, so empty still means "the whole root" and no real
default call is refused. `test_agent_verification_gate.py`'s module docstring
encoded the refuted claim and is corrected rather than left contradicting the
code.

`test_agent_verification_gate.py` varied only `root`; it now varies the `path`
axis in both the mutation and no-mutation branches, each with a control.

## S8 — the existence oracle

Reproduced: unauthorized **missing** -> `ValueError: not a directory: <resolved>`;
unauthorized **existing** -> `PermissionError: ...: <resolved>`. Two
distinguishable answers, both echoing the real host path, and every server
wrapper returns `"ERROR: %s" % exc` to a confined agent. Authorization now runs
first and its refusal names no path, so both cases return byte-identical text.
An authorized root may still report "not a directory" with its path.

**One test broke and it encoded the defect as the requirement.**
`test_harness_dev.test_resolve_root_invalid` passed `/nonexistent/path/abc123xyz`
— unauthorized *and* missing — and asserted "not a directory", pinning the
oracle. Read before touching. Its real intent is kept, asked from inside the
authorized tree; the unauthorized half moved to the confinement file.

## Mutation results — every new guard planted, observed failing, reverted

| guard | mutation | result |
|---|---|---|
| S4 blind-catalog | (written against unfixed code) | 2 failed, 23 passed |
| S1 write-arm scope | (written against unfixed code) | 4 failed, 40 passed |
| S3 `_resolve_target_path` | escape check -> `if False:` | 4 failed (all four tools) |
| S2 `path` narrowing | narrowing -> `if False:` | 2 failed, 1 passed (control held) |
| S8 order + echo | restored stat-then-authorize + `% resolved` | 1 failed |

## Verbatim pytest lines

Files chosen deliberately — `scripts/select_regression_tests.py` was **not**
used (its `changed_diff()` never diffs a commit range, so committed work is
invisible to it, and its selection is driven by common English tokens).

RED, at the final item count:

```
S4  2 failed, 23 passed in 2.32s      tests/test_risk_of_fail_closed.py
S1  4 failed, 40 passed in 3.17s      tests/test_harness_root_confinement.py
S3  4 failed, 4 passed, 44 deselected in 3.18s   (-k "second_path or path_inside")
S2  2 failed, 1 passed, 45 deselected in 1.19s   (-k narrowing)
S8  1 failed, 169 passed, 7 skipped in 16.05s    (the encoded-defect test)
```

GREEN, final, 23 named files:

```
868 passed, 7 skipped in 43.43s
```

`test_risk_of_fail_closed test_permission_modes test_permission_gate_coverage
test_permission_gate_dispatch test_permission_gate_http
test_permission_policy_display test_permission_rules test_reloadable_mcp
test_harness_root_confinement test_harness_dev test_harness_git
test_harness_misc test_harness_build_diff test_agent_dispatch_dev_tools
test_agent_verification_gate test_grounded_outcomes_agent_dispatch
test_autopilot_controller test_autopilot_server test_content_digest
test_log_inspect test_project_detect test_symbol_index
test_command_router_catalog`

The full suite (~522s) was not run.

## Commits

```
9ced1b6  Make a blind classifier blind however it went blind (S4)
c7de416  Confine the write arm too, and cover it (S1)
efb8a57  Resolve the path contradiction: confine it, then read it (S2 + S3)
c9a83c8  Authorize before stat-ing, and stop echoing unauthorized paths (S8)
```

## S5 — merge hazard, recorded not fixed. Instructions for the integrator.

Nothing was cherry-picked. Corrected from the brief: the two fixes are on
**two different branches**.

### 1. `278839e` — alias-key laundering, on `work/13-drift-family` only

`tests/test_advertised_surface_drift.py` does **not exist** on this lineage —
absent from the shared base `feat/verified-fetch-modes-calibration`, from
`work/18-riskof-location`, and from HEAD. Presence measured across branches:

| branch | file | carries `278839e`? |
|---|---|---|
| `feat/verified-fetch-modes-calibration` | absent | — |
| `work/18-riskof-location`, HEAD | absent | — |
| `work/13-drift-family` | present | **yes** |
| `work/16-dead-vocab` | present | no (pre-fix) |
| `work/20-standing-planmode` | present | no (pre-fix) |

So the hazard is not "HEAD carries the hole", it is that **the file arrives from
whichever lane lands it**, and two of the three carry the pre-fix version. The
`work/13` and `work/20` versions differ by **21 insertions / 209 deletions**, so
a conflict resolution of "take theirs" silently drops the 23-line fix.

*Carry:* `tests/test_advertised_surface_drift.py` must land in its
**`work/13-drift-family`** form. The load-bearing addition is
`test_agent_tool_alias_keys_and_targets_are_both_real`, which asserts
`set(server._AGENT_TOOL_ALIASES) - capabilities.dispatch_names(server._agent_dispatch) == []`.
The allowance in `test_no_surface_advertises_an_unregistered_tool` subtracts
alias *keys* while only *targets* were ever validated.

*Verify after merging:* inject a ghost and watch it fail.

```python
server._AGENT_TOOL_ALIASES["__ghost_manifest__"] = "memory_search"
# and advertise __ghost_manifest__ on tool_manifest()
```

Then run `pytest tests/test_advertised_surface_drift.py`. It must report
**1 failed**, naming `['__ghost_manifest__']`. Green with the ghost injected
means the pre-fix file won the merge. Remove the injection and confirm green.
**I could not run this reproduction here — the guard file does not exist on this
branch.** Reported as unverified rather than restated as measured.

### 2. `dfad7ce` — error-signal ratchet, on `work/16-dead-vocab` only

*Carry:* the `server.py` half renaming `_agent_run_tool_policy_error` to
`_agent_run_tool_refusal` and returning short gate names, plus its
`tests/test_advertised_surface_drift.py` hunk — which **also touches the file
above**, so both fixes contend for the same file and must be merged together,
not sequentially with "take theirs".

*Verify after merging:* `python scripts/check_error_signals.py` must exit **0**.
Read the exit status **without a pipe** — the sweep agent first read `tail`'s
status instead of the script's and caught it. Use
`python scripts/check_error_signals.py > out.txt 2>&1; echo $?`.

## NEW findings

**Important — the error-signal ratchet is already RED on this lineage, at a
site `dfad7ce` does not fix.** Run unpiped on this branch:

```
REAL EXIT=1
legacy ERROR: signal ratchet failed; remove/migrate sites, do not add or swap them
server.py:14557: unexpected return_literal_prefix in _agent_dispatch
  (1 present, baseline allows 0): "ERROR: read-only agent run has no
  host-selected project root, ..." % tool_name
```

This is **not** the sweep's finding 8 (4 findings in `_agent_run_tool_policy_error`
on `work/20`) and **not** mine: my `server.py` hunks are all at line 15934+, and
the flagged literal is present at base `3e3ae60`. It was introduced by last
night's `2cec327` — a fix that added a new stringly `ERROR:` site in the same
commit that closed the rootless-dispatch hole. `dfad7ce` renames a *different*
function and will not clear it. Left unfixed deliberately: the literal is pinned
by `test_agent_dispatch_dev_tools.test_read_only_dispatch_reaches_test_discover`
("no host-selected project root"), so migrating it is a contract change outside
this brief's five items. **An integrator should not expect `dfad7ce` alone to
turn the ratchet green.**

**Important — S3 is an execution escape, not only a write escape.** The sweep
filed `harness_tools.py:411` as `lint_run(..., fix=True)` writing outside the
authorized root. Measured, `test_run` with the pytest framework **executes**
attacker-chosen code outside every authorized root and that code wrote a file
there. The class is arbitrary code execution from a confined agent, and it
applied to every direct `@mcp.tool()` caller and every unbound run, not just the
project-bound agent path.

**Recorded, not fixed — `build_run` still verifies nothing.** Reading `path`
does not close the sweep's stated consequence, because `build_run` takes
`root` + `command` and no `path`: `build_run(root=proj, command="git --version")`
still exits 0 and sets `verification_ok` for a change it never examined. The
`path` axis was the assigned fix and is done; this residue needs a different
control (relating the command to the work) and is left as a filed item rather
than guessed at.
