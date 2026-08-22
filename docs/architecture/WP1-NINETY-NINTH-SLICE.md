# WP1 Ninety-Ninth Slice — Metrics implementation ownership

This slice completes the metrics platform-boundary move. The implementation
now lives in `sonder_runtime.platform.metrics`; the root `sonder_metrics.py`
module is an identity-preserving compatibility shim for external callers and
older deployment surfaces.

The move preserves:

- `MetricsRegistry` and `default_registry` object identity across both import paths;
- the process-local default registry and import-time optional Prometheus behavior;
- metric names, fixed label sets, no-op fallback behavior, and rendering;
- existing root imports without allowing package code to depend on the root module.

Evidence is recorded by `tests/test_metrics_boundary.py` and the architecture,
compile, requirement-evidence, and diff gates.
