# Identical failed tool retry guard

This slice implements the Issue #510 section 6/7 candidate guard at the
autonomous agent's host dispatch loop. After a tool returns a failed host
observation, the loop records a fingerprint containing the canonical tool
name, resource, host-selected project scope, arguments, and returned failure
text. The same canonical call may be attempted twice. A third unchanged
failure is refused before dispatch and the loop reports a recovery direction.

Changing the tool, resource, scope, or arguments starts a separate allowance.
A successful call clears the failed state for that identity. The guard is
per-run and in-memory; it does not replace provider transport retry policy,
idempotent read semantics, or durable worker retry/reconciliation.

## Evidence

- `tests/test_retry_guard.py` is a deliberate canary for the complete failure
  fingerprint, bounded refusal, changed recovery, and success reset.
- `tests/test_agent_tools.py::test_agent_stops_repeating_identical_failed_tool_call`
  exercises the production `_agent_impl` loop and proves only two failed
  dispatches occur before the refusal path.
- `python -m pytest tests/test_retry_guard.py tests/test_agent_tools.py -q`
  passed (`104 passed` when run together; the agent tool file alone passed
  `99 passed`).
- `python -m compileall -q server.py sonder_runtime/application/agents/retry_guard.py`
  passed.

This does not claim completion of every Issue #510 guard candidate or a
durable cross-restart retry ledger.
