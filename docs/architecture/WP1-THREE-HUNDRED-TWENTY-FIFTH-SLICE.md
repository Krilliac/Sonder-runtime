# WP1 Three-Hundred-Twenty-Fifth Slice — fanout model health

## Boundary

The advisory model-health recording and cooldown policy for fanout
failures (`_fanout_health`) now lives in
`sonder_runtime/adapters/fanout_health.py` as `record_health`, with the
provider-hint cloud cooldowns, the hourly-capped exponential local backoff
and the caller-error exemption unchanged. It persists through the packaged
fanout store, reads the transport's `ModelCallError` and renders through the
packaged `fanout_failures.safe_error`, so the adapters layer is its home. The
cloud classifier is injected as `is_cloud_model_name`; `server.py` keeps
`_fanout_health` as a thin delegate passing `_is_cloud_model_name` at call
time, so the existing worker monkeypatch seams keep working.

## Evidence

- `tests/test_fanout_health_boundary.py` verifies the root delegate's classifier seam, the success record, cloud provider-hint cooldowns, exponential local backoff with its cap and first-failure delay, and that caller errors stay eligible.
- `python -m pytest -q tests/test_fanout_health_boundary.py tests/test_model_fanout.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
