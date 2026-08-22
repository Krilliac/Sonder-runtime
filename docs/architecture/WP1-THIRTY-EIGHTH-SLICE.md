# WP1 Thirty-Eighth Slice: Activity-Status Formatter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The authorized activity-status renderer moved from the server composition root
to `sonder_runtime.adapters.observability.activity_formatting`. Source
selection and authorization remain at the server/interface boundary; the
adapter only renders the already-authorized snapshot.

## Evidence

- Activity-redaction, activity-verdict, server-helper, and lifecycle HTTP
  regressions: **268 passed**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

Agent-scoped activity selection and command orchestration remain in the server;
only the deterministic activity snapshot renderer moved here.
