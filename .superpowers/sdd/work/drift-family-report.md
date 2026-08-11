# Advertise-vs-dispatch drift family (#16, #22, #32)

Worktree `D:\sonder-wt\13-drift-family`, branch `work/13-drift-family`, based on
`fix/cloud-help-drift` @ `b8a15ef` (itself off `feat/verified-fetch-modes-calibration`
@ `9f377f1`). Nothing here is compared against `main`.

## 0. Method

Every number below is derived programmatically from source in this checkout, never
restated from the filed report. Four extractors, each proved non-vacuous:

| Quantity | Extractor | Non-vacuity proof |
|---|---|---|
| registered MCP tools | live `server.mcp._tool_manager._tools` | cross-checked against an independent AST sweep for `@mcp.tool()` on module-level defs; both give **184**, symmetric difference empty |
| dispatchable agent tools | `tool_capabilities.dispatch_names(server._agent_dispatch)` — literal `tool_name == "x"` / `tool_name in {...}` comparisons | **117** branches; `_agent_dispatch` was read to its final line and ends `return "ERROR: unknown tool '%s'."` — there is **no** fallthrough, so a name without a branch is genuinely unreachable |
| advertised help names | `- name: {...}` line parser | probe entry injected into each surface, parser still sees it |
| loop action types | AST over `_loop_dispatch`, grouped **per branch** so aliases are not double-counted | **68** branches / **85** distinct names; floors asserted |

### Coverage of the pre-existing drift check (asked for explicitly)

`tool_capabilities.validate_shadow()` / `format_shadow_report()` prints
`ok (N descriptors; shadow-only)` while `_DESCRIPTORS` contains **15** entries against
**184** registered MCP tools — **8.2 % of the surface**. It is a shadow slice by design
and says so, but any "ok" it prints must be read as covering 8 % of the tools, not the
tool surface. None of the three defects here is inside its slice, so it printed `ok`
throughout. The new guard does not depend on it.

## 1. Re-measured numbers (mine govern where they disagree)

### #16 — autopilot

Filed: *"advertises 18 tools it cannot call (18 of 42)"*.

The filed pair is arithmetically correct **for the wrong denominator**. 18 of 42 is
exactly the literal `_AUTOPILOT_WORKSPACE_TOOLS` extras block (42 names, 18 with no
dispatch branch). But that block is not the surface: `_AUTOPILOT_WORKSPACE_TOOLS` is
`_AUTOPILOT_OBSERVE_TOOLS | {extras}`, and it is **the whole union** that
`_agent_impl` renders verbatim into the model transcript as
`HOST TOOL ALLOWLIST (cannot be expanded by the model):` and that
`_autopilot_plan_model` renders again as `Allowed tools: ...`. Measuring the literal
block undercounts the promise by 5.

| Surface | Advertised | Dispatchable | Gap |
|---|---|---|---|
| `_AUTOPILOT_WORKSPACE_TOOLS` (workspace policy) | **86** | **63** | **23** |
| `_AUTOPILOT_OBSERVE_TOOLS` (observe policy) | **45** | **40** | **5** |

Gap (workspace, 23) = `apply_patch, build_clean, build_run, dependency_add,
dependency_audit, dependency_remove, dependency_update, diff_files, find_references,
format_code, git_branch, git_checkout, git_cherry_pick, git_commit, git_merge,
git_stash, git_tag, lint_run, rename_symbol, secret_scan, test_discover, test_run,
typecheck_run`. Gap (observe, 5) is the subset `dependency_audit, diff_files,
find_references, secret_scan, test_discover`.

**Disagrees with the filed figure: 23/86 and 5/45, not 18/42.**

### #22 — names advertised but never registered as MCP tools

Filed: *"12 names advertised but never registered as MCP tools at all"*.

Measured across every surface that names tools to a model or operator:

| Surface | Advertised names | Not a registered MCP tool |
|---|---|---|
| `AGENT_TOOL_HELP` | 130 | **0** |
| `REPOSITORY_AGENT_TOOL_HELP` | 55 | **0** |
| `REPOSITORY_READ_ONLY_TOOLS` | 55 | **0** |
| `_AUTOPILOT_OBSERVE_TOOLS` | 45 | **0** |
| `_AUTOPILOT_WORKSPACE_TOOLS` | 86 | **0** |
| union of the above | 131 | **0** |
| `tool_manifest()` | 141 | **3** |

The three are `save`, `run`, `delete`, produced by the shorthand manifest key
`"workflow_list/save/run/delete"`. `tool_manifest()`'s keys are slash-separated tool
names everywhere else in the dict (and `tool_capabilities._manifest_has` parses them
that way), so that key reads as four tool names, three of which no `@mcp.tool()` backs.

**Disagrees with the filed figure: 3, not 12, and on `tool_manifest()` rather than any
agent help surface.**

A count of 0 on five surfaces is the classic early-abort tell, so it was checked before
being believed: the registration extractor agrees exactly with the live MCP tool
manager (184 = 184), and the *same* extractor did surface the 3 real hits on
`tool_manifest()` — so it is not blind. Other candidate surfaces were swept and cleared:
`workflows.json` (6 action types, all implemented), the 271-entry slash-command registry
(carries no tool names), `command_catalog`, `slash_menu`, `intents`, `command_router`,
and the nine `_AGENT_TOOL_ALIASES` (all resolve to registered tools). No surface anywhere
produces 12.

Also recorded, not fixed: 46 registered tools are absent from `tool_manifest()` — the
reverse (hidden-capability) direction. Out of scope for this lane.

### #32 — loop actions

Filed: *"58 loop actions advertised, 85 implemented"*.

Both raw numbers reproduce exactly. The 27-name difference does **not**, because 17 of
the 27 are aliases of already-advertised branches (`assetgen` → `artifact_generate`,
`agent_status` → `master_status`, `work`/`agent` → `workbench_agent`, …).

| Quantity | Measured |
|---|---|
| `_loop_dispatch` branches | **68** |
| distinct action names accepted (incl. aliases) | **85** |
| names advertised by the `"Valid action types: ..."` reply | **58** |
| names advertised by the `loop()` docstring examples | **35** |
| advertised but not implemented (both surfaces) | **0** |
| **branches with no advertised name at all** | **10** |

The 10 genuinely hidden capabilities: `workbench_agent` (aliases `work`, `agent`),
`directory_create`, `file_read_range`, `artifact_risk_inspect`, `process_list`,
`process_memory_risk_inspect`, `data_inspect`, `checklist_create`, `checklist_update`,
`checklist_show`.

**Partially disagrees: real hidden capability is 10 branches, not 27 names.**

### Top-level surface (context only, deliberately not touched)

`AGENT_TOOL_HELP`: **130 advertised / 107 dispatchable / gap 23** — the same 23 names.
The prior lane's `115 / 92 / 23` has the right gap and the wrong totals. Not fixed here:
`_KNOWN_UNDISPATCHABLE_HELP_ENTRIES` in `tests/test_agent_help_dispatch_drift.py`
already parks exactly these 23 as a known allowance, and editing `AGENT_TOOL_HELP` in
`server.py` risks colliding with the sibling lane that owns it. Left untouched and
unchanged.

## 2. What was fixed

Following the `b8a15ef` precedent: where a tool could not be made dispatchable *safely*,
it was removed from the advertisement rather than given a dispatch branch.

1. **`_AUTOPILOT_WORKSPACE_TOOLS`** — dropped the 18 workspace-only undispatchable
   names (`test_run`, `lint_run`, `format_code`, `typecheck_run`, three `dependency_*`,
   seven `git_*`, `build_run`, `build_clean`, `rename_symbol`, `apply_patch`). They have
   no `_agent_dispatch` branch, so a run that believed the allowlist spent steps on
   `ERROR: unknown tool` — removal costs zero capability. They remain direct MCP tools,
   and `workspace_run` / `script_run` still reach the same build/test binaries through
   the argv-checked execution path.
2. **`_AUTOPILOT_OBSERVE_TOOLS`** — dropped the 5 undispatchable names, same reasoning
   (four of them are the ones `b8a15ef` had already removed from
   `REPOSITORY_READ_ONLY_TOOLS` for lack of root confinement; `dependency_audit` joins
   them). Result: 45 → 37, gap 0.
3. **`process_list` / `process_memory_risk_inspect` / `task_progress` moved from the
   observe set into the workspace-only block** — see the new finding in §5. No capability
   lost: workspace runs keep all three, observe runs never actually had them.
4. **`tool_manifest()`** — `"workflow_list/save/run/delete"` →
   `"workflow_list/workflow_save/workflow_run/workflow_delete"`.
5. **loop vocabulary** — introduced `_LOOP_ACTION_TYPES`, one canonical name per
   `_loop_dispatch` branch (68). The unknown-action reply is now rendered from it
   (`", ".join(_LOOP_ACTION_TYPES)`) instead of a hand-maintained literal, and the
   `loop()` docstring gained an `All valid \`type\` values:` line carrying the same 68.
   The docstring's example block was relabelled from "Supported action types" (which
   implied exhaustive at 35 of 68) to "Argument shapes, by example".

### What was deliberately left

- **The 17 loop aliases stay unadvertised.** #32's direction is reversed, and the brief
  was not to blindly advertise all 27. Advertising both `assetgen` and
  `artifact_generate` would suggest two capabilities where there is one. Aliases keep
  working; the guard asserts every *branch* has a name, not every *name*.
- **No new dispatch branch was added anywhere.** Every one of the 23 undispatchable
  tools takes a `root` argument resolved by `harness_tools._resolve_root`, which does no
  allowed-roots check — the precise reason `b8a15ef` removed four of them rather than
  wiring them up. Adding a branch would hand an autonomous run unconfined filesystem
  access.
- **`AGENT_TOOL_HELP` and `_KNOWN_UNDISPATCHABLE_HELP_ENTRIES`** — a separate filed item,
  another lane's file region.
- **The 46 tools missing from `tool_manifest()`** — hidden-capability direction, not this
  defect family.

## 3. Regression guard

`tests/test_advertised_surface_drift.py` (new, 10 tests). It recomputes both sides from
source on every run — registration from the live tool manager, dispatchability from
`_agent_dispatch`, loop vocabulary from `_loop_dispatch`'s branch table — and asserts:

- no advertising surface (both help constants, all four `_agent_tool_help` flag
  combinations, `REPOSITORY_READ_ONLY_TOOLS`, both autopilot allowlists,
  `tool_manifest()`) names anything that is not a registered MCP tool;
- the alias allowance in that test cannot launder a fake name — every
  `_AGENT_TOOL_ALIASES` target must itself be registered;
- both autopilot allowlists are subsets of `_agent_dispatch`'s branches;
- the observe allowlist is a subset of `REPOSITORY_READ_ONLY_TOOLS`;
- the loop reply is rendered from `_LOOP_ACTION_TYPES`, every branch has an advertised
  name, and neither the reply nor the docstring names an unimplemented action;
- `test_extractors_cannot_go_vacuous` asserts floors (≥150 registered, ≥90 dispatch
  branches, ≥50 loop branches, ≥100 manifest names), the AST-vs-runtime registration
  agreement, and a probe entry through the manifest parser.

Two existing tests encoded the old (unreachable) placement of the process tools and were
updated with an explanation rather than deleted:
`tests/test_process_risk_server.py::test_process_tool_registration_help_reload_and_autopilot`
now additionally asserts observe policy *denies* them and that
`_agent_dispatch(..., read_only=True)` returns the repository read-only refusal;
`tests/test_tool_capabilities.py::test_local_read_only_project_dedup_and_autopilot_sets_are_unchanged`
now checks the workspace set for those two.

### Mutation proof (each applied, run, reverted)

| Mutation | Result |
|---|---|
| add `"__ghost_tool__"` to `_AUTOPILOT_OBSERVE_TOOLS` (advertised, never registered) | **3 failed** — `test_no_surface_advertises_an_unregistered_tool`, `test_autopilot_allowlists_only_name_dispatchable_tools`, `test_autopilot_observe_allowlist_survives_repository_read_only_policy` |
| re-add `"test_run"` to `_AUTOPILOT_WORKSPACE_TOOLS` (registered, undispatchable) | **1 failed** — `test_autopilot_allowlists_only_name_dispatchable_tools` |
| drop `"checklist_show"` from `_LOOP_ACTION_TYPES` | **2 failed** — `test_loop_advertises_every_action_type_it_implements`, `test_loop_docstring_and_error_reply_advertise_the_same_vocabulary` |

All three reverted; suite back to green. The guard binds in both directions.

## 4. Test evidence (verbatim pytest summary lines)

RED — `tests/test_advertised_surface_drift.py`, before any production change:

```
FAILED tests/test_advertised_surface_drift.py::test_extractors_cannot_go_vacuous
FAILED tests/test_advertised_surface_drift.py::test_no_surface_advertises_an_unregistered_tool
FAILED tests/test_advertised_surface_drift.py::test_autopilot_allowlists_only_name_dispatchable_tools
FAILED tests/test_advertised_surface_drift.py::test_autopilot_observe_allowlist_survives_repository_read_only_policy
FAILED tests/test_advertised_surface_drift.py::test_loop_error_message_is_rendered_from_the_action_vocabulary
FAILED tests/test_advertised_surface_drift.py::test_loop_advertises_every_action_type_it_implements
FAILED tests/test_advertised_surface_drift.py::test_loop_advertises_no_action_type_it_does_not_implement
FAILED tests/test_advertised_surface_drift.py::test_loop_docstring_and_error_reply_advertise_the_same_vocabulary
8 failed, 2 passed in 1.46s
```

GREEN — the same file after the fixes:

```
10 passed in 1.24s
```

GREEN — every test file in `tests/` that references `_AUTOPILOT_*`, `tool_manifest`,
`AGENT_TOOL_HELP`, `_loop_dispatch` or `workflow` (49 files, named explicitly; the full
suite was **not** run):

```
912 passed, 1 skipped, 1 warning in 56.97s
```

Files run: `test_advertised_surface_drift, test_agent_help_dispatch_drift,
test_agent_tools, test_archive_create, test_archive_tools,
test_artifact_grounding_server, test_artifact_risk_server, test_autopilot_controller,
test_autopilot_server, test_autopilot_snapshot_migration, test_autopilot_store,
test_command_router_catalog, test_content_digest, test_context_pack,
test_control_plane_protection, test_data_convert, test_data_query,
test_dependency_inventory_server, test_eval_history, test_file_batch_write,
test_file_transfer_server, test_git_history, test_git_tools,
test_hardware_profile_server, test_inspection_facade, test_isolated_run_server,
test_json_patch_server, test_live_reload, test_local_service_probe_server,
test_log_inspect, test_master_orchestrator, test_mcp_dependency, test_mcp_primitives,
test_memory_maintenance, test_memory_tools, test_permission_modes,
test_process_risk_server, test_project_detect, test_self_heal,
test_sqlite_mutate_server, test_symbol_index, test_task_state, test_text_patch,
test_tool_capabilities, test_unsafe_lab, test_workbench_server,
test_workflow_usecases, test_workflows, test_workspace_compare`.

## 5. New findings

**IMPORTANT (new, fixed here).** Observe-policy autopilot advertised three tools that a
second gate always refused. `_autopilot_work_model` runs observe tasks with
`read_only=True`, and `_repository_read_only_error` refuses every tool outside
`REPOSITORY_READ_ONLY_TOOLS`. `process_list`, `process_memory_risk_inspect` and
`task_progress` are dispatchable and were in `_AUTOPILOT_OBSERVE_TOOLS`, but are
deliberately **not** in `REPOSITORY_READ_ONLY_TOOLS` — so they were rendered into the
observe transcript, passed `_autopilot_tool_policy`, and then died at the read-only gate.
This is a second layer of the same shape that a dispatch-only check cannot see, and the
old tests asserted the broken arrangement as intentional (`test_process_risk_server.py`
even asserted `_autopilot_tool_policy({"policy": "observe"})("process_list", …) == ""`
while `test_process_risk_server.py:27` separately asserted `_agent_dispatch(read_only=True)`
denies it). Fixed by moving the three into the workspace-only block; the observe
allowlist is now asserted to be a subset of `REPOSITORY_READ_ONLY_TOOLS`.

**IMPORTANT (new, reported, not fixed).** The filed figures for two of the three defects
were derived from a *sub-surface* rather than the surface actually shown to the model
(#16: the 42-name extras literal, not the 86-name union that is rendered into the
transcript) or counted *names* where the unit of capability is a *branch* (#32: 27
names, 10 real capabilities). Both errors bias the same way as the already-known
`115/95/7` error. Any remaining figure in this defect family should be re-derived before
use.

**MINOR (new, fixed).** `tool_manifest()` was the only surface in the codebase naming
tools that no `@mcp.tool()` backs (`save`, `run`, `delete`). It is also the least
covered: 46 registered tools appear nowhere in it.

## 6. Commits

- `277fd27` — Stop advertising tools and loop actions that cannot run, and guard both
  (autopilot allowlists, `tool_manifest` key, `_LOOP_ACTION_TYPES`, new guard file,
  two updated existing tests)
- this report committed separately

Checkout left clean on `work/13-drift-family`. Nothing pushed. `git stash` was never
run; `refs/stash` untouched. Staging was always by explicit path.
