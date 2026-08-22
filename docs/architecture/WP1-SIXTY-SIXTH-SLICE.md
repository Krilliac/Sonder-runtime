# WP1 Sixty-Sixth Slice: Evaluation History Path Boundary

## Scope

The packaged `evaluation_history_store` adapter now consumes state locations
through `sonder_runtime.platform.paths`, the canonical package path boundary.
That boundary re-exports the existing `sonder_paths` implementation, so the
default home resolution and `SONDER_EVAL_HISTORY` override remain unchanged.
This is a caller-only migration; the root path implementation and migration
behavior are untouched.

## Evidence

- Evaluation-history regression tests pass, including identity with the
  packaged path boundary.
- `python -m compileall -q sonder_runtime server.py` passes.
- `scripts/check_architecture.py` passes.
- `scripts/check_requirement_evidence.py` passes.
- `git diff --cached --check` and `git diff --check` pass.

## Remaining boundary

Other packaged callers still use compatibility-backed path imports and require
separate behavior-preserving slices. Persistence, launchers, HTTP/REPL, the
server composition root, and strangler services are outside this slice.
