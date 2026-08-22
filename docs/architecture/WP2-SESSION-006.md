# WP2 SESSION-006 — Event-boundary session forking

`sonder_runtime.application.session.fork` provides a pure application seam
for planning a session fork. `fork_session(events, boundary, ...)` validates a
single typed `SessionId` stream with contiguous sequences, requires an
explicit inclusive `ForkBoundary`, and records the exact boundary sequence and
event ID in immutable `SessionLineage` metadata.

The result contains the immutable source prefix (`inherited_events`) and the
typed child ID. Source `DomainEvent` objects remain unchanged and retain the
parent aggregate ID. The module emits no events, writes no storage, and does
not invoke replay; a repository adapter may later materialize the returned
plan. A missing, out-of-range, mismatched, gapped, duplicated, cross-session,
or untyped boundary/stream fails closed with an application error.

This slice intentionally does not modify repository, replay, event-vocabulary,
or specification files. Persistence materialization, child-stream projection,
repair, and external API exposure remain later work.

## Evidence

- `tests/test_session_fork.py`
- `python -m pytest tests/test_session_fork.py tests/test_session_replay.py -q`
- `python -m compileall -q sonder_runtime`
