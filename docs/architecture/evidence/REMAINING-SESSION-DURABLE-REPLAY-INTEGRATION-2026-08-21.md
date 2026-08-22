# Durable session replay snapshot consistency

## Slice

`crash_safe_replay` now requires the repository's integrity report to match
the exact event snapshot returned for replay: checked count, first sequence,
and last sequence must agree. This closes the separate-read consistency gap at
the `SessionRepository` port/SQLite adapter boundary; an append or mismatched
adapter report can no longer make a shorter prefix appear crash-safe.

## Evidence

- `tests/test_remaining_session_durable_replay.py`
  - rejects a report covering more events than the replay read;
  - rejects a stale report covering fewer events than the replay read.
- Focused command: `python -m pytest -q tests/test_remaining_session_durable_replay.py tests/test_remaining_session_002_006.py`
- Static checks: `python -m compileall -q sonder_runtime/application/session/durable_replay.py tests/test_remaining_session_durable_replay.py`
- Formatting check: `git diff --check`
