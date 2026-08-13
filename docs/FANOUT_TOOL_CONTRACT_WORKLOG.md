# Tool-Contract Conformance — Work Log

Branch: `agent/fable-tool-contract`. Companion design:
`FANOUT_TOOL_CONTRACT_DESIGN.md`. This log exists so a resumed session can
continue without re-deriving anything; update it at every milestone with
*verified* results only — a number that was not measured does not belong here.

## Environment (verified 2026-08-12)

- Worktree: `D:\sonder-wt\fable-tool-contract` (one of many agent worktrees;
  never `git stash` here — `refs/stash` is shared across worktrees).
- Tests: `C:/Users/natew/.claude/mcp-servers/sonder-runtime/venv/Scripts/python.exe -m pytest tests/... -q`
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
- [ ] Focused suites green; architecture / error-signal / privacy gates
      green; broad relevant suite green — record the actual commands and
      counts here when they have run.
- [ ] Adversarial self-review of the diff (privilege widening, endpoint
      drift, data exposure, races, test-only enforcement).
- [ ] Final DoD evidence recorded here; branch committed and clean.

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

## Next steps (for a resumed session)

1. Run the three gate scripts + the broad suite; record real counts here.
2. Adversarial diff review; final DoD evidence section here.
