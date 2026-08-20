# WP1 Sixty-Third Slice: Packaged Entrypoint Configuration Boundary

Status: implemented on `agent/wp1-execution-status`.

The packaged `python -m sonder_runtime` entrypoint now consumes configuration
through `sonder_runtime.platform.config`. The boundary re-exports the
historical `sonder_paths` module attribute used by the entrypoint, preserving
configuration loading, error types, defaults, and user-global path discovery.

## Scope

This slice migrates only `sonder_runtime/__main__.py`. The HTTP interface,
REPL, `server.py`, command catalog, persistence, launchers, and
`strangler_services.py` are unchanged.

## Evidence

- Entrypoint configuration-boundary regression tests pass.
- `python -m compileall -q sonder_runtime server.py` passes.
- `scripts/check_architecture.py` passes.
- `scripts/check_requirement_evidence.py` passes.
- `git diff --cached --check` and `git diff --check` pass.

The platform module remains compatibility-backed by the root implementation;
this slice removes one production package caller from the root import surface.
