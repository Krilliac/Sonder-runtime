# Bounded production session continuity — 2026-08-21

This slice connects the existing typed SESSION-006/007/009/010 contracts to
the canonical application graph without changing extension or selfmod lanes.

- `SessionContinuityService` reads a bounded SQLite snapshot, verifies the
  complete append-only hash chain, and then delegates fork and repair planning
  to the pure application contracts. A corrupt or over-bound stream fails
  closed; repair never replays an in-flight effect.
- The application graph exposes one cached continuity service backed by the
  same session repository used by chat capture. Checkpoints are source-pinned
  to logical session events, so checkpoint and privacy metadata appended to the
  stream do not advance the projection source identity.
- HTTP session routes expose read-only repair, fork planning, and checkpoint
  inspection. Existing authentication and administrator gates remain in the
  server boundary; no new write route is added.
- Retention execution is owner-gated and append-only. It records bounded
  `session.retention.applied` markers instead of updating/deleting source
  events. Query/export applies those markers before the existing recursive
  redactor, so expired or redaction-required content is not returned publicly.
  Re-execution is idempotent for already-marked targets; malformed markers
  fail closed.

Focused validation:

```text
python -m pytest -q tests/production/test_session_continuity_wiring.py tests/test_session_fork.py tests/test_session_repair.py tests/test_session_checkpoint_privacy_integration.py tests/test_http_session_facade.py tests/test_http_session_integration.py tests/production/test_application_session_wiring.py
python -m compileall -q sonder_runtime
```

Deliberate non-goals: child-stream materialization, physical deletion,
extension/selfmod integration, and Git/commit operations.
