# WP2 SESSION-006/009 — lineage and projection checkpoints

`fork_session` requires an explicit event boundary and returns immutable
parent/child lineage metadata plus the bounded source prefix. `ProjectionCheckpoint`
binds a versioned projection to the exact source sequence and hash, allowing
startup to reject stale projections while retaining the event stream as source
of truth.

Evidence: `tests/test_session_fork_checkpoint.py`, architecture/evidence gates,
and compileall.
