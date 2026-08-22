# WP2 SESSION-009 — Versioned projection checkpoints

`sonder_runtime.application.session.checkpoints` adds an application-only
checkpoint value for a session projection. `ProjectionCheckpoint` stores the
projection, a positive `projection_version`, and the exact source
`source_sequence` and `source_hash` consumed to produce it.

`checkpoint_projection(events, source_hash, projection_version=1)` builds the
existing deterministic projection and pins it to the caller's source digest.
`is_stale(sequence, hash)` returns true when either source identity differs;
`require_fresh` raises `IntegrityFailure` for the same condition. Checkpoints
are frozen and validate that `source_sequence` agrees with the projection's
last sequence.

The module has no repository, replay, clock, cache, or write-side dependency.
It does not persist checkpoints or calculate a repository hash; the caller or
adapter supplies the source hash obtained from its source-of-truth boundary.

## Evidence

- `tests/test_session_checkpoints.py`
- `python -m pytest tests/test_session_checkpoints.py tests/test_session_replay.py -q`
- `python -m compileall -q sonder_runtime`
