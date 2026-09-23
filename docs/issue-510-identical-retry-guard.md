# Existing identical failed-call guard: live canary

The autonomous agent loop in `server.py` already counted failed calls by its
canonical tool-and-arguments signature before this PR. After two failed
dispatches of the same call, it refuses the third dispatch and gives the
model a recovery instruction; a further unchanged attempt ends the run.
The count does not depend on the returned error text.

`tests/test_agent_tools.py::test_agent_retry_guard_blocks_same_call_when_failure_text_changes`
is a deliberate production-loop canary: the dispatcher returns a different
request ID in each failure, yet only two dispatches occur. It complements
the existing constant-error and semantic no-progress tests in that file.

This is verification of an existing per-run guard, not a new runtime retry
policy. It does not prove durable cross-restart retry control, duplicate-worker
prevention, fanout limits, context-growth control, or the other Issue #510
guards. Issue #510 remains open.
