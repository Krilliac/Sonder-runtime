# Tool-Contract Conformance — Work Log

Branch: `agent/fable-tool-contract`. Companion design:
`FANOUT_TOOL_CONTRACT_DESIGN.md`. This log exists so a resumed session can
continue without re-deriving anything; update it at every milestone with
*verified* results only — a number that was not measured does not belong here.

## Environment (verified 2026-08-12)

- Worktree: `D:\sonder-wt\fable-tool-contract` (one of many agent worktrees;
  never `git stash` here — `refs/stash` is shared across worktrees).
- Tests: `python -m pytest tests/... -q` (from the runtime virtual environment)
  with cwd = this worktree (verified: `server`/`sonder_runtime` resolve to the
  worktree, pytest 9.1.1; `tests/test_permission_gate_coverage.py` +
  `tests/test_tool_capabilities.py` → 42 passed in 6.59s).
- Milestone gates: `scripts/check_architecture.py`, `check_error_signals.py`,
  `check_history_privacy.py`.
- Commits: DCO-signed (`git commit -s`), scoped; never commit
  `FABLE_CONTINUOUS_DIRECTIVE.md`.

## Checklist

- [x] Research: surfaces, gates, catalog, permission modes, HTTP roles,
      loop/workflow path, redaction, existing test corpus (see design doc).
- [x] Design doc under `docs/`.
- [x] `tool_contract.py` classifier module.
- [x] Conformance tests `tests/test_tool_contract_conformance.py` (P1–P8),
      RED first against the live holes, then green with the enforcement.
- [x] E1: deny-by-default for unbound system operations at
      `sonder_serve._http_tool_refusal`.
- [x] E2: derived loop-payload closure replacing
      `_LOOP_GLOBAL_OPERATION_TYPES`.
- [x] E3: activity-ledger sensitive-key vocabulary aligned with the text
      regex.
- [x] Focused suites green; architecture / error-signal / privacy gates
      green; broad suite green apart from three pre-existing
      machine-timing failures (evidence in M4).
- [x] Adversarial self-review of the diff (privilege widening, endpoint
      drift, data exposure, races, test-only enforcement) — notes in M4.
- [x] Final DoD evidence recorded here; branch committed and clean.

## Milestones

### M1 — research + design (this commit)

Findings that drive the work (full detail and file anchors in the design
doc):

- Live bypass: `/loop` action spellings of `memory_privacy_repair` /
  `memory_quality_repair` reach the tools for ordinary served accounts while
  the direct `/<tool>` spellings require the developer role
  (`sonder_serve._LOOP_GLOBAL_OPERATION_TYPES` names only 4 of the 7
  loop-reachable system operations). Read from source; to be reproduced RED
  by the conformance tests before the fix.
- `admin_accounts` (agent-refused system operation) reaches its tool body for
  ordinary served accounts over HTTP; only the in-tool admin-token check
  stops it. Same RED-first plan.
- No executable parity exists between `sonder_serve.SYSTEM_OPERATION_TOOLS`,
  `server._AGENT_SYSTEM_OPERATOR_TOOLS`, and
  `permission_modes.DURABLE_AUTHORITY_TOOLS`.
- Redaction: `activity_tracker._safe_args`'s key vocabulary misses `pwd`,
  `passwd`, `credential`, `authorization`, `access_key`, `apikey` (the
  free-text regex covers them), so `{"pwd": ...}` survives into the
  in-memory, detail-gated activity ledger.
- `loop` and `workflow_run` are NOT agent-dispatchable (verified: no
  `tool_name == "loop"` branch in `server.py`; `workflow_run`'s branch is
  behind the system-operator refusal) — loop actions enter via operator
  surfaces only, so the closure belongs at the HTTP boundary.
- Existing coverage worth not duplicating: `test_system_operation_roles.py`
  (role matrix per operation, catalogued bypass check for *bound* tools),
  `test_permission_gate_http.py` (mode/rule gate at `_handle_slash`, no
  auth-context coverage), `test_permission_gate_dispatch.py` (agent, loop,
  console, MCP decide()-level), `test_permission_gate_coverage.py` (branch →
  map completeness floor), `test_activity_redaction.py` (redactor shapes).

### M2 — classifier + conformance harness + E1/E2/E3 (verified)

RED first (run 2026-08-13, `pytest tests/test_tool_contract_conformance.py`):
`9 failed, 3 passed` — the failures, verbatim reasons:

- `test_every_system_operator_tool_is_refused_for_an_ordinary_account` —
  "admin_accounts is agent-refused as a system operation but sails past the
  HTTP boundary for an ordinary served account" (H1, live).
- `test_loop_action_spelling_carries_the_same_role_as_the_tools_own_name` —
  `memory_privacy_repair`: `_loop_global_operation_refusal` returned `""`
  (H2, live). Same for `self_heal_repair` under a developer context.
- `test_the_ledger_masks_every_name_the_text_redactor_treats_as_secret` —
  `{"pwd": "hunter2-value"}` survived `_safe_args` verbatim (H4, live);
  `--pwd` argv value survived `_safe_command`.
- Four `ModuleNotFoundError: No module named 'tool_contract'`.

Slice 2 RED (same day): `4 failed, 25 passed` — `validate_contracts`/
`contracts` missing, and the unbound rule initially swallowed the
durable-authority refusal's actionable text for `admin_login`
("administrator authorization is required for an unclassified system
operation" instead of the console/allow-rule remedy) — fixed by letting
durable tools fall through to `decide()`.

GREEN (after `tool_contract.py`, E1, E2, E3, and binding
`admin_accounts -> account_management`): `29 passed` in the conformance
file. Affected-suite sweeps, all with the venv interpreter from this
worktree:

- `test_system_operation_roles + test_activity_redaction +
  test_activity_verdict + test_permission_gate_http +
  test_permission_gate_dispatch + test_risk_of_fail_closed +
  test_permission_gate_coverage + test_workflows` → **237 passed**.
- `test_advertised_surface_drift + test_memory_maintenance +
  test_workbench_server + test_serve_auth + test_permission_modes +
  test_permission_durable_authority + test_tool_capabilities +
  test_policy_explain + test_app_permission_surface +
  test_read_only_agent_policy + test_permission_rules` →
  **422 passed, 1 failed**: `test_serve_auth.py::
  test_query_string_does_not_change_openai_route_or_terminal_metric`
  (socket `TimeoutError`; passes in isolation in 6.10s — load flake on this
  16 GB box, route untouched by this diff; re-checked in the broad run).

### M3 — diagnostics visibility + packaged payload (verified)

RED first: `test_tool_contract_ships_in_the_packaged_payload` and
`test_diagnostics_reports_contract_drift_without_enforcement` both failed
(`tool_contract.py` absent from `REQUIRED_FILES`; no
`tool_contract_report()` in `diagnostics`). GREEN after
`server.tool_contract_report()` + the diagnostics line + the packager
entry: conformance + tool-capabilities files → **64 passed**; packager
tests (`-k "required or payload or manifest"`) → **7 passed**;
`-k diagnostics` across the tree → **10 passed, 1 skipped**.

### M4 — final verification (2026-08-13)

Commands run from this worktree with its runtime virtual environment's
`python` executable:

- `scripts/check_architecture.py` → rc=0.
- `scripts/check_error_signals.py` → rc=0 (no new `ERROR:`-literal returns;
  the drift report's `ERROR …` form matches the shadow report's shape).
- `scripts/check_history_privacy.py` → rc=0 ("known debt only (7
  object/path pair(s))" — pre-existing).
- Broad suite `python -m pytest -q` (tests + proposals) →
  **3 failed, 7226 passed, 43 skipped, 15 warnings in 1431s (23:51)**.
  The three failures are wall-clock timing assertions in
  `tests/test_sonder_storage.py` (probe-kill latency budgets of 0.5s/1.0s
  vs ~1.2–2.4s measured). They are environmental, not from this branch:
  the branch's diff touches no storage file, the failing test file is
  byte-identical to the pre-change `policy-explain-preflight` worktree,
  and the same three tests fail there identically under today's load.
  The `test_serve_auth` query-string timeout flake seen in one focused
  run did not recur in the broad run.
- `git diff --check ab071fa..HEAD` → clean. All commits DCO-signed.

Adversarial diff review (privilege widening / endpoint drift / exposure /
races / test-only enforcement):

- The classifier canonicalizes spellings before lookup; no
  `SYSTEM_OPERATION_TOOLS` key appears as an alias key, so canonicalization
  can only find MORE bindings, never fewer. The derived loop closure
  refuses a strict superset of the old hand map (all four old entries
  still resolve to the same admin operation).
- New refusals apply only to served non-admin auth contexts;
  `context=None`, `local-open`, owner api-key, console, and direct MCP
  paths are byte-for-byte unchanged in behavior (pinned by tests).
- Durable-authority tools keep `decide()`'s actionable refusal rather than
  gaining a role wall (message quality, not a widening; explicit operator
  allow-rules keep their documented effect).
- Redaction change only widens masking (observability cost, never a
  privacy cost), matching the module's own stated failure direction.
- All new checks are pure dict/frozenset reads before dispatch — no state
  writes, no timing dependence, no TOCTOU pair.
- Enforcement lives in the production gates; only the authority
  name-grammar tripwire is test-only, by design and documented.

## Definition-of-Done evidence map

- Contract source + enforcement points documented/traceable → design doc
  surfaces table; `tool_contract.py` docstrings name the exact call sites
  (`_http_tool_refusal`, `_loop_global_operation_refusal`); diagnostics
  line makes drift operator-visible.
- Every registered/reachable tool classified or deliberately rejected →
  `contracts()` covers registered ∪ declared ∪ loop-canonical names;
  `risk_of` grades every catalogued spelling; unknown names are refused on
  MCP (`ToolError`), agent (`ERROR: unknown tool`), HTTP catalogued
  (`None` → never dispatched), and non-interactive `decide()`
  (`UNCLASSIFIED` → deny) — all pinned in the conformance file.
- Shared accounts cannot reach privileged actions through synonyms /
  workflows / indirect / catalog routes → P1 sweep over
  `_AGENT_SYSTEM_OPERATOR_TOOLS` at the real boundary; loop-action
  spelling closure; fully-bound slash-alias sweep; saved-workflow replay
  probe; role matrix per bound tool.
- Local-open usable where intended → role boundaries stay silent for
  local-open/api-key on every system tool (bound and unbound); plan and
  deny rules still bind there.
- Malformed/unknown input fails before effectful dispatch → unknown-kwarg
  ValueError with sentinel handler, non-dict agent args, MCP schema
  validation, unknown-name refusals.
- Suites/gates → M4 numbers above.
- `git diff --check` clean; DCO on every commit; this log records the
  verified commands, results, limitations, and next steps.

Branch left committed and clean; not merged. Commits (post-rebase hashes;
the branch was rebased onto `a4b3760`, which merged PRs #173/#174, after
M4's first pass): `959d42c` (design), `ffaeb9d` (harness + E1/E2/E3),
`eb3dcbb` (workflow-store test isolation), `6548b56` (diagnostics +
packager), `d95460b`/`c6b2e4e` (work-log evidence), plus the final
evidence commits below.

### M5 — re-verification on the rebased base (2026-08-13)

The rebase put PRs #173 (fanout selection profiles) and #174 (chat
response receipts — touches `sonder_serve.py`) beneath this branch.
Verified on the rebased tree:

- Enforcement intact: `tool_contract` import, E1/E2 blocks, and the
  `admin_accounts` binding all present; `git diff a4b3760..HEAD --stat`
  matches the branch's 8 files exactly.
- Focused suites (conformance + serve_auth + system-operation roles +
  gate-http + gate-dispatch + gate-coverage + activity-redaction +
  tool-capabilities + packager) → **372 passed, 1 failed** (the
  `test_serve_auth` query-string timeout; investigation below).
- Gates: architecture rc=0, error-signal rc=0, history-privacy rc=0.

`test_query_string_does_not_change_openai_route_or_terminal_metric`
investigation (it began failing consistently ~01:10): the route answers
in 1.30s outside pytest on this tree; interleaved paired sampling
(mine/base alternating, 3 rounds) then failed on BOTH this branch and the
`request-trace-receipt` worktree, whose `sonder_serve.py` hash equals the
merge-base's — i.e. pure pre-change code fails identically. The failure
is a 5s client-socket budget missed during first-request startup in a
fresh pytest process while the box sits at 2.1G free RAM of 15.3G
(fleet-preflight CAUTION). Environmental, base-equivalent, not from this
branch. Same family as the `test_sonder_storage.py` budgets; both belong
to a machine-load flake backlog, not to this change.

A second full-suite pass on the rebased tree was started and then
deliberately aborted: a concurrent session began working in this same
worktree (commit `52d4abf`, uncommitted live-reload/selfmod wiring for
`tool_contract`, and a parallel pytest), so the run would have measured a
moving tree. Rebased-tree assurance therefore rests on the 372-test
focused pass plus the three gates above, with the full-suite green from
M4 on identical branch content. Next session: run `python -m pytest -q`
once the tree settles, alongside whatever the concurrent work adds.

## Residual limitations / next steps

- The authority name-grammar tripwire is a floor, not a proof: a
  privileged tool named entirely outside the vocabulary
  (`admin_*`, `permission_*`, `runtime_policy_*`, `autopilot_*`,
  `workflow_*`, `memory_privacy_*`, `memory_quality_*`, plus the exact
  names in `_AUTHORITY_GRAMMAR_NAMES`) and left out of every declared set
  still needs a human to classify it.
- The three `test_sonder_storage.py` timing budgets fail on this loaded
  16 GB box on pre-change code too; if they persist on a quiet machine,
  they deserve their own issue.
- One focused seven-file run immediately after a `git commit` showed
  `3 failed, 145 passed`, including `inspect.getsource(server.diagnostics)`
  returning a *different function's* source — the signature of the live
  server module being hot-swapped mid-run. The same selection then passed
  four consecutive times (148 passed) and the broad suite covered the same
  files green. Plausible mechanism: git touching CRLF-normalized files
  changes the on-disk digest the live-reload watcher compares. Not caused
  by this branch (the watcher predates it); worth its own investigation if
  it recurs — a test-session guard that pins `SONDER_LIVE_RELOAD=0` would
  remove the class.
- `tool_capabilities.py` descriptor completion remains future work by
  design (non-goal).

### M6 — JSON-encoded argv redaction (verified 2026-08-22)

The previously open `workspace_run`/`script_run` activity-rendering gap is
closed. Their `args_json` input is a JSON string, and serializing that string
again produced a JSON string literal such as
`python "[\"--token\", \"secret\"]"`; the activity argv redactor could not
recognize the flag/value pair in that shape. The server activity projection
now decodes valid JSON lists before rendering, while invalid input retains the
existing text-redaction path.

Evidence:

- `tests/test_activity_redaction.py` proves both `workspace_run` and
  `script_run` render structured argv and mask their secret values.
- Final activity-redaction suite: **25 passed**.
- Related activity/agent suite: **138 passed**.
- Architecture, error-signal, and history-privacy gates pass with no new
  history debt.
