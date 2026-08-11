# Task #9 — merge `sdd/02-calibration` into `work/12-merge-dispatch`

Branch: `work/12-merge-dispatch` (was `b47b3ca` = `sdd/01-permission-gate`)
Merged: `sdd/02-calibration` @ `5164b79`
Common base: `9f377f1` (`feat/verified-fetch-modes-calibration`) — **not** `main`.

---

## 1. Merge status and conflict resolution

```
$ git merge --no-commit --no-ff sdd/02-calibration
Auto-merging server.py
Automatic merge went well; stopped before committing as requested
```

**Textual conflicts: 0.** Nineteen paths staged; `server.py` was the only file
both lanes touched and git auto-merged it.

That zero is the finding, not the reassurance. The two lanes edit `server.py`
in disjoint regions — Lane 1 wired the gate at the *top* of `_agent_dispatch`
(`_agent_permission_gate_error`, server.py:14537) and rewrote the slash-command
catalog; Lane 2 appended 23 `if tool_name == ...` branches to the *bottom* of
the same function and extended the project-scoping tables. Git had no reason to
stop, so nothing forced a human to look at whether the gate Lane 1 installed
covers the doors Lane 2 opened. Every resolution below was therefore verified by
execution rather than accepted from the merge driver.

### Resolutions checked on their merits

Because there were no conflict hunks, "resolution" here means confirming the
auto-merge preserved each side's intent rather than silently dropping one. Three
things were checked:

| Property | Lane 1 alone | Lane 2 alone | Post-merge | Verdict |
|---|---|---|---|---|
| `_agent_dispatch` literal tool branches | 117 | 140 | **140** | Lane 2's 23 additions all survived |
| `_agent_permission_gate_error` call inside `_agent_dispatch` | present | absent | **present** (server.py:14537) | Lane 1's gate survived |
| Floor test `test_permission_gate_coverage.py` | 8 passed | n/a | **8 passed** | Lane 1's floor survived |

Dispatch-name sets were extracted with an AST walk mirroring
`tool_capabilities.dispatch_names()` (scratch script `dispatch_names.py`, applied
to `git show <rev>:server.py`). The merged set is **byte-identical** to Lane 2's
set (`diff` produced no output), and Lane 1 added **zero** dispatch branches
(117 at base, 117 at `b47b3ca`) — Lane 1's server.py work was gate wiring, not
new doors. So the two lanes' `server.py` changes are genuinely orthogonal at the
dispatch table, and the auto-merge is correct on that axis.

No hunk was resolved by preferring one side; nothing needed to be.

---

## 2. The load-bearing question: does Lane 1's floor still bind?

**Answer: the floor still passes, and it still binds — but not on the surface
Lane 2 changed. It never covered that surface, before or after the merge.**

### Post-merge run

```
$ python -m pytest tests/test_permission_gate_coverage.py -q
........                                                                 [100%]
8 passed in 4.29s
```

Identical to the pre-merge baseline (`8 passed in 4.45s`). Passing is not
evidence, so it was mutated twice.

### Mutation A — does the floor bind at all?

Added an unmapped slash branch to `control_command`:

```python
if cmd == "/mutationprobe":
    return "probe"
```

```
E       AssertionError: 1 dispatch branch(es) resolve to no tool, so the gate is consulted,
E       receives an empty set, and allows them:
E           /mutationprobe (server.py, console)
FAILED tests/test_permission_gate_coverage.py::test_every_dispatch_branch_is_in_the_map_or_declared_display_only
1 failed, 7 passed in 10.56s
```

**The floor binds.** It is not vacuous and it is not passing on air.

### Mutation B — does it bind on *Lane 2's* surface?

Restored `server.py` (sha256 verified), then added an unmapped branch to
`_agent_dispatch` — the function Lane 2 actually edited:

```python
if tool_name == "mutation_probe_tool":
    return "probe"
```

The probe is a real, unmapped dispatch branch, confirmed on both counts:

```
merged dispatch names: 141          # was 140
mutation_probe_tool                 # present in the extracted set

risk_of(mutation_probe_tool) = ask
decide(interactive=False) allowed = True | reason = no interactive prompt available;
    ask tools are not blocked outside the console | risk = ask
```

That is precisely the shape Lane 1's floor exists to catch: a branch the map
cannot see, whose gate decision comes back **allowed**. And:

```
$ python -m pytest tests/test_permission_gate_coverage.py -q
........                                                                 [100%]
8 passed in 3.58s
```

**The floor does not notice.** It stays green with an ungated door open.

### Why — and why the stated hazard was mis-located

The floor's chain list (`tests/test_permission_gate_coverage.py:_CHAINS`) is:

```python
_CHAINS = (
    ("server.py",       "control_command", "console"),
    ("sonder_repl.py",  "main",            "console"),
    ("sonder_serve.py", "_handle_slash",   "http"),
)
```

Three **slash-command** chains. `_agent_dispatch` is not among them, and the two
maps the floor checks (`command_catalog.console_tools()`,
`http_slash_tools()`) are slash-name maps keyed `/name`, not agent tool maps.

So the task's stated hazard — "the floor was built against Lane 1's tool set,
not the post-merge one" — is real in effect but wrong in mechanism. The floor's
tool set did not go stale. The floor was **never pointed at the agent dispatch
surface at all**. Lane 2 could add any number of ungated agent tools and Lane 1's
completeness floor would stay green, before the merge and after it.

This is not a regression the merge introduced, and it is not a defect in Lane 1's
floor as scoped — it is a **coverage gap between the two lanes** that only becomes
load-bearing once Lane 2's 23 branches land. Recommendation in §5.

---

## 3. Tools Lane 2 made reachable, and whether the gate covers them

### Count: **23** (measured, agrees with the stated "23")

Derivation: AST-extract literal `tool_name ==` / `tool_name in (...)` comparisons
inside `_agent_dispatch` from `git show <rev>:server.py`, then set-difference.

```
9f377f1 (base) : 117
b47b3ca (lane1): 117
5164b79 (lane2): 140
merged         : 140
```

`comm -13 base lane2` yields exactly 23 names, and 140 − 117 = 23 corroborates
the set difference (the set difference is the measurement; the subtraction is
only a cross-check). `comm -23` shows Lane 2 **removed** nothing.

### Gate coverage: all 23 are graded — none hits the fail-open default

The agent-path gate is name-based: `_agent_permission_gate_error` →
`permission_modes.decide(name, interactive=False)`. Coverage therefore means the
tool resolves to a real risk grade rather than `permission_modes.risk_of`'s
unknown-tool fallback of `"ask"`, which `interactive=False` degrades to *allow*.

```
apply_patch          risk=mutation   manual_allowed=True  plan_action=deny
build_clean          risk=execution  manual_allowed=True  plan_action=deny
build_run            risk=execution  manual_allowed=True  plan_action=deny
dependency_add       risk=execution  manual_allowed=True  plan_action=deny
dependency_audit     risk=execution  manual_allowed=True  plan_action=deny
dependency_remove    risk=execution  manual_allowed=True  plan_action=deny
dependency_update    risk=execution  manual_allowed=True  plan_action=deny
diff_files           risk=safe       manual_allowed=True  plan_action=allow
find_references      risk=safe       manual_allowed=True  plan_action=allow
format_code          risk=execution  manual_allowed=True  plan_action=deny
git_branch           risk=mutation   manual_allowed=True  plan_action=deny
git_checkout         risk=mutation   manual_allowed=True  plan_action=deny
git_cherry_pick      risk=dangerous  manual_allowed=True  plan_action=deny
git_commit           risk=mutation   manual_allowed=True  plan_action=deny
git_merge            risk=dangerous  manual_allowed=True  plan_action=deny
git_stash            risk=mutation   manual_allowed=True  plan_action=deny
git_tag              risk=mutation   manual_allowed=True  plan_action=deny
lint_run             risk=execution  manual_allowed=True  plan_action=deny
rename_symbol        risk=mutation   manual_allowed=True  plan_action=deny
secret_scan          risk=safe       manual_allowed=True  plan_action=allow
test_discover        risk=safe       manual_allowed=True  plan_action=allow
test_run             risk=execution  manual_allowed=True  plan_action=deny
typecheck_run        risk=execution  manual_allowed=True  plan_action=deny

ungraded (risk_of=='ask', the fail-open default): NONE
```

All 23 carry a catalog grade, because they were already direct MCP tools with
`command_catalog` entries — Lane 2 gave them a dispatch branch, not a new
identity. `manual_allowed=True` across the board is by design (Lane 1's docstring:
with nobody at a keyboard, only `plan` mode and an explicit per-tool `deny` rule
stop a dispatch); `plan_action` shows the gate genuinely refusing all 19
mutation/execution/dangerous tools.

**So the gate covers all 23 — but it was luck of the catalog, not a check.**
Nothing in either lane's tests asserts this property, and Mutation B proves the
floor would not have caught it had any of the 23 lacked a catalog entry.

---

## 4. Task #34 — the four repository tools

`fix/cloud-help-drift` @ `b8a15ef` removed four tools from
`REPOSITORY_READ_ONLY_TOOLS` and `REPOSITORY_AGENT_TOOL_HELP` rather than making
them dispatchable. Its commit message states the reason exactly:

> Also drop **test_discover, find_references, diff_files and secret_scan** [...]
> They are read-only but cannot be made dispatchable as they stand:
> `harness_tools._resolve_root` resolves any absolute path with no allowed-roots
> check [...] **Adding a dispatch branch would have handed a read-only agent
> unconfined filesystem read.** With no dispatch branch they were already
> unreachable, so removal costs no capability.

The four tools are **`test_discover`, `find_references`, `diff_files`,
`secret_scan`**.

`harness_tools._resolve_root` is unchanged post-merge and still performs no
confinement:

```python
def _resolve_root(root):
    p = Path(root or ".").resolve()
    if not p.is_dir():
        raise ValueError("not a directory: %s" % p)
    return p
```

### All four appear in Lane 2's added set

`fix/cloud-help-drift` is **not** an ancestor of this merge, so the four are
still present in `REPOSITORY_READ_ONLY_TOOLS` (server.py:13537) — and Lane 2 has
now given each of them the dispatch branch that commit said must never exist.

### Post-merge status, measured

Probed against a **tiny scratch directory outside the repo** (never the real home
directory), seeded with a canary AWS key:

```
=== read_only=True, NO project root (repository_extra_roots="") ===
  secret_scan      ALLOWED  secret scan: 1 finding(s) in 2 files scanned
                              canary.py:1  [AWS credential]  AWS_SECRET_ACCESS_KEY = "AKIA..."
  test_discover    ALLOWED  test discovery: pytest   tests: 0
  find_references  ALLOWED  references to 'Canary'   total: 1   other.py:1  def Canary():
  diff_files       ALLOWED  diff --git "a/C:\Users\natew\AppData\Local\Temp\...\canary.py"

=== read_only=True, project root = repo ===
  secret_scan      REFUSED  ERROR: agent project path rejected: path is outside the host-selected project root
  test_discover    REFUSED  ERROR: agent project path rejected: path is outside the host-selected project root
  find_references  REFUSED  ERROR: agent project path rejected: path is outside the host-selected project root
  diff_files       REFUSED  ERROR: agent project path rejected: repository root is outside the host-selected project root
```

| Tool | Post-merge status |
|---|---|
| `test_discover` | Re-added to dispatch. Confined **only** when a project root is set. Unconfined otherwise. |
| `find_references` | Re-added to dispatch. Confined **only** when a project root is set. Unconfined otherwise. |
| `diff_files` | Re-added to dispatch. Confined **only** when a project root is set. Unconfined otherwise. Also leaks the absolute host path into the diff header. |
| `secret_scan` | Re-added to dispatch. Confined **only** when a project root is set. Unconfined otherwise — **and it is the one that prints the secrets it finds**. |

### Credit where due: Lane 2 did add real confinement

Lane 2 is not naive here. It fixed the precise second defect
`fix/cloud-help-drift` named — that `_project_scoped_path_key` addressed a
`"path"` key these tools never receive instead of the `"root"` they actually take
— and added all 23 tools to `_PROJECT_SCOPED_PATH_TOOLS`, plus a new
`_PROJECT_SCOPED_ROOT_AND_PATH_TOOLS` set for the tools taking both. Its own
comment shows it understood the stakes:

> Developer-workflow tools (harness_tools.py) all take "root", not "path" --
> without this, a project-bound run would silently rebase a nonexistent "path"
> key while the real "root" argument (and its escape-check) went untouched.

That work is correct and the right-hand column above proves it binds. The gap is
that **the confinement is conditional on a project root existing**, and the
read-only agent path does not require one.

### The unconfined path is reachable, not theoretical

`_agent_dispatch_observed` forwards the project scope verbatim
(`repository_extra_roots=project`, server.py:15892), and an empty project is a
legal, non-erroring input:

```
_agent_project_scope('') -> ('', '')
```

`_agent_impl` accepts `project=""` with no error and no requirement that a
read-only run carry a root. Autopilot reaches the same shape directly
(server.py:18326: `read_only=(run.get("policy") == "observe" and not unsafe)`
paired with `project=run.get("project", "")`).

Confirmed end-to-end through the production wrapper, not just the inner function:

```
_agent_dispatch_observed(secret_scan, project='') ->
secret scan: 1 finding(s) in 2 files scanned
  canary.py:1  [AWS credential]  AWS_SECRET_ACCESS_KEY = "AKIAIOSFODNN7EX...
```

The permission gate does not stop it: all four are graded `safe`, so
`plan_action=allow` — they are permitted in **every** mode. Confinement was the
only control, and it is absent on this path.

**This is a live capability regression introduced by the merge.** At base the
four had no dispatch branch and were genuinely unreachable, exactly as
`fix/cloud-help-drift` asserted. After this merge, a read-only "observe" agent
run with no project argument can read and exfiltrate any absolute path on the
host — `secret_scan(root="C:/Users/natew")` would walk the home directory and
print what it finds.

---

## 5. Test results

Full suite (~522s) deliberately not run. Files run, verbatim summaries:

**Lane 1 scope** — `test_permission_gate_coverage.py`, `test_permission_gate_dispatch.py`,
`test_permission_gate_http.py`, `test_permission_modes.py`,
`test_permission_policy_display.py`, `test_serve_history.py`:

```
368 passed in 88.74s (0:01:28)
```

**Lane 2 scope** — `test_agent_dispatch_dev_tools.py`, `test_agent_verification_gate.py`,
`test_agent_tools.py`, `test_end_report_standing.py`, `test_grounded_outcomes.py`,
`test_grounded_outcomes_agent_dispatch.py`, `test_learning_health.py`,
`test_server_source_invariants.py`, `test_tool_capabilities.py`,
`test_autopilot_server.py`, `test_workbench_server.py`:

```
379 passed in 10.04s
```

**Both together in one process** (cross-file `permission_modes` state leakage is
itself a merge hazard; the floor module carries a leak guard for exactly this):

```
747 passed in 75.37s (0:01:15)
```

747 = 368 + 379, so no test was lost or skipped when the suites share a process,
and no state leaked between them.

---

## 6. Findings

### CRITICAL — the four repository tools are dispatchable and unconfined without a project root

Detail in §4. Lane 2 re-added `test_discover`, `find_references`, `diff_files`
and `secret_scan` to `_agent_dispatch`, which `fix/cloud-help-drift` @ `b8a15ef`
had explicitly removed on the grounds that a dispatch branch "would have handed a
read-only agent unconfined filesystem read". Lane 2's confinement covers the
project-bound case correctly but not the rootless one, and the rootless one is
reachable from autopilot's `observe` policy. Verified by execution against a
scratch directory, both at `_agent_dispatch` and at `_agent_dispatch_observed`.

**Recommend this merge does not land as-is.** Options, cheapest first:

1. Require a project root on the read-only path — make `_agent_dispatch` refuse
   any tool in `_PROJECT_SCOPED_PATH_TOOLS` when `read_only=True` and
   `repository_extra_roots` is empty. One check, closes all 23 at once, not just
   the four.
2. Add allowed-roots confinement inside `harness_tools._resolve_root`, which is
   where the root cause actually lives and which would also protect direct MCP
   callers.
3. Drop the four dispatch branches and take `fix/cloud-help-drift`'s removal.

Option 1 is the smallest change that makes the merge safe; option 2 is the
correct long-term fix. Not implemented here — choosing between them is a policy
decision on another lane's work, and it needs its own RED test.

### IMPORTANT — Lane 1's completeness floor does not cover `_agent_dispatch`

Detail in §2, proven by Mutation B. The floor covers three slash-command chains
and stays green with an ungated agent-tool branch present. Lane 2's 23 tools all
happen to be graded, so nothing is currently ungated — but that is a property of
the catalog, not a property any check enforces, and it is precisely the
"the floor stopped looking at the right set" shape this project keeps hitting.

Recommend extending `_CHAINS` with a fourth entry for
`("server.py", "_agent_dispatch", <agent tool grading>)` — asserting every name
in `tool_capabilities.dispatch_names(_agent_dispatch)` resolves to a
`risk_of` other than the `"ask"` fallback. Mutation B is a ready-made RED for it.

### MINOR — `diff_files` leaks absolute host paths

Its output embeds the fully-resolved host path in the `diff --git` header
(visible in the §4 probe output), so even a confined call discloses where the
project lives on disk. Cosmetic next to the Critical, noted for completeness.

---

## Provenance

Produced 2026-08-11 in worktree `D:\sonder-wt\12-merge-dispatch` on branch
`work/12-merge-dispatch`. No `git stash` was run; no `git add -A` was run; the
stash refs were not touched. `sdd/01-permission-gate` and `sdd/02-calibration`
were not modified. Nothing was pushed. Mutation experiments were applied to
`server.py` and reverted from a byte-exact backup, verified with
`sha256sum -c` (`server.py: OK`) before the merge was committed. Probe files were
written only to the session scratchpad, never inside the repository.
