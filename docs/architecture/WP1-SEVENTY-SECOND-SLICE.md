# WP1 Seventy-Second Slice: Metrics Boundary Caller Migration

Status: implemented on `agent/wp1-execution-status`.

## Scope

The packaged HTTP lifecycle adapter now imports `MetricsRegistry` through
`sonder_runtime.platform.metrics`, the canonical packaged platform boundary.
That module re-exports the existing root implementation and default registry,
so metric names, label sets, enablement behavior, and rendered semantics are
unchanged. The root `sonder_metrics` module remains the implementation and
compatibility boundary for legacy callers.

This slice changes one packaged caller and adds focused identity and metric
surface regressions. `server.py`, persistence, the command catalog, launchers,
HTTP/REPL interfaces, and `strangler_services.py` are unchanged.

## Evidence

- `tests/test_metrics_boundary.py`: focused boundary and metric-surface tests pass.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check` and `git diff --check`: pass.

## Remaining boundary

`sonder_runtime.platform.metrics` still re-exports the root implementation.
The root `sonder_metrics` allowance cannot be removed until the implementation
and remaining root callers are migrated without changing metric semantics.
