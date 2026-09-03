# WP1 Three-Hundred-Forty-Fifth Slice — fanout worker id deduplication

## Boundary

Rewired `server._fanout_worker_id` to delegate to the existing
`sonder_runtime/domain/fanout_worker_identity.fanout_worker_id` instead of
duplicating its logic inline.

The root `server._fanout_worker_id` is a compatibility delegate that binds
the module-level `_FANOUT_WORKER_INSTANCE`, `os.getpid()`, and
`threading.get_ident()`. The `FANOUT_WORKER_INSTANCE` constant import
already existed from slice 338; this slice removes the redundant body.

## Evidence

- `tests/test_fanout_worker_id_boundary.py` verifies domain function format,
  server delegate uses domain function, and server delegate output format.
- `python -m pytest -q tests/test_fanout_worker_id_boundary.py` — 3 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
