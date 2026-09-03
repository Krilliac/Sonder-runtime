# WP1 Three-Hundred-Seventh Slice — agent activity command rendering

## Boundary

The activity-ledger rendering of an agent tool call (`_agent_activity_command`)
and its argument normalizers (`_activity_argv`, `_agent_argv`,
`_batch_agent_operations`) now live in
`sonder_runtime/domain/agents/activity_command.py` as `activity_command`,
`activity_argv`, `agent_argv` and `batch_operations`, with every tool-family
branch and the argv round-trip guard unchanged. `server.py` keeps all four
root names as identity-preserving alias imports, so the agent dispatcher, the
batch-write path and the activity tracker call the same objects.

## Evidence

- `tests/test_agent_activity_command_boundary.py` verifies the four alias identities, argv normalization (including the non-list and unserializable cases), batch-operation decoding, and every tool-family rendering branch.
- `python -m pytest -q tests/test_agent_activity_command_boundary.py tests/test_activity_redaction.py tests/test_agent_tools.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
