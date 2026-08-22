# WP2 SESSION-007 — Session tail repair and resume planning

`sonder_runtime.application.session.repair` diagnoses a session's ordered
event tail and returns a read-only `SessionRepairPlan`. The plan identifies the
maximal valid prefix, the first sequence available to a fresh attempt, and the
events that must not be replayed automatically.

Structural gaps, duplicate sequences, invalid sequence values, and events from
another session are inconsistent and are not resumable. A structurally valid
stream ending in an effectful request/start without its completion or failure
is a truncated tail; the valid boundary is immediately before that operation.
Completed events remain in the safe prefix. This makes the resume boundary
explicit and prevents a repair caller from issuing an already-issued model or
tool side effect a second time.

The module is pure application logic. It does not mutate event objects or the
input iterable, invoke replay, append or delete repository records, emit
events, or alter repository/replay modules. Materializing a repair and starting
a new attempt are caller-owned operations.

## Evidence

- `tests/test_session_repair.py`
- `python -m pytest tests/test_session_repair.py tests/test_session_fork.py tests/test_session_replay.py -q`
- `python -m compileall -q sonder_runtime`
