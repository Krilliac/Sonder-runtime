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
- [ ] `tool_contract.py` classifier module.
- [ ] Conformance tests `tests/test_tool_contract_conformance.py` (P1–P8),
      RED first against the live holes, then green with the enforcement.
- [ ] E1: deny-by-default for unbound system operations at
      `sonder_serve._http_tool_refusal`.
- [ ] E2: derived loop-payload closure replacing
      `_LOOP_GLOBAL_OPERATION_TYPES`.
- [ ] E3: activity-ledger sensitive-key vocabulary aligned with the text
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

## Next steps (for a resumed session)

1. Write `tool_contract.py` (classifier; see design "The contract model").
2. Write `tests/test_tool_contract_conformance.py`; run it and record the
   RED failures for H1/H2/H4 here verbatim.
3. Implement E1/E2/E3; record the same tests green.
4. Run focused + gate scripts + broad suite; record real counts.
5. Adversarial diff review; final DoD evidence section here.
