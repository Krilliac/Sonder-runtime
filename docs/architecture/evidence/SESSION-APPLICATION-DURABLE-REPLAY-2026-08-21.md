# Session application durable replay integration

## Slice

The production `Application` composition graph now has an end-to-end evidence
test at `tests/test_session_application_integration.py`. It invokes the graph's
canonical `ChatService`, verifies that one completion commits the ordered
`model.requested`, `user.message`, and `model.response` stream to the configured
SQLite database, then rebuilds the application and replays the same stream from
the reopened repository. The test also proves provider failure is fail-closed:
the failed completion appends no events to the existing stream.

## Evidence

- Focused command: `python -m pytest -q tests/test_session_application_integration.py`
- Static check: `python -m compileall -q sonder_runtime/application/session tests/test_session_application_integration.py`
- Formatting check: `git diff --check`

This slice is limited to the session application/adapter/bootstrap graph, its
test, and this evidence record; it does not exercise or modify context, OpenAI,
MCP, jobs, or root migration behavior.
