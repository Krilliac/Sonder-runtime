# WP1 Three-Hundred-Fifteenth Slice — loop action resolution

## Boundary

The loop dispatcher's action resolution now lives in
`sonder_runtime/domain/loop_actions.py`: `LOOP_ACTION_TOOLS` (the action
names that are not tool names), `loop_action_tool` (the tool the permission
gate decides on, canonicalized through the packaged tool naming) and
`loop_verdict_result` (ok only when the output starts with the success
prefix), all unchanged. The verdict takes the base text-result builder as an
injected `text_result` callable.

`server.py` keeps `_LOOP_ACTION_TOOLS` and `_loop_action_tool` as
identity-preserving alias imports (the HTTP serve layer and `tool_contract`
reach them through the root names) and `_loop_verdict_result` as a thin
delegate injecting `_loop_text_result`. `_loop_text_result` and
`_LOOP_ACTION_TYPES` deliberately did not move: the former's
`startswith("ERROR:")` parse is recorded in the shrink-only error-signal
baseline under its current scope, and the latter is the dispatcher's own
accepted-action surface.

## Evidence

- `tests/test_loop_actions_boundary.py` verifies the alias identities, action-to-tool resolution including canonical aliases, the verdict through an injected text result, and the root delegate on the server text result.
- `python -m pytest -q tests/test_loop_actions_boundary.py tests/test_permission_gate_dispatch.py tests/test_risk_of_fail_closed.py tests/test_tool_contract_conformance.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
