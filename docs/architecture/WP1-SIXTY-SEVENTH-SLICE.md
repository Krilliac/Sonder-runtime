# WP1 Sixty-Seventh Slice: Embedding Cache Path Boundary

## Scope

The packaged `embedding_cache` adapter now obtains its database location from
`sonder_runtime.platform.paths`, the canonical package path boundary. The
boundary re-exports the existing `sonder_paths.state_path` implementation, so
default resolution and the `SONDER_EMBED_CACHE_DB` override are unchanged.
This is a caller-only migration; the root path implementation and cache
behavior are untouched.

## Evidence

- Embedding-cache regression tests pass, including the packaged-boundary
  delegation contract.
- `python -m compileall -q sonder_runtime server.py` passes.
- `scripts/check_architecture.py` passes.
- `scripts/check_requirement_evidence.py` passes.
- `git diff --cached --check` and `git diff --check` pass.

## Remaining boundary

Other packaged callers still use compatibility-backed path imports and require
separate behavior-preserving slices. Persistence, launchers, HTTP/REPL, the
server composition root, and strangler services are outside this slice.
