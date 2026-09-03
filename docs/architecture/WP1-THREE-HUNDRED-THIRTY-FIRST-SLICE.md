# WP1 Three-Hundred-Thirty-First Slice — fanout residency fence

## Boundary

The no-load fence that rechecks a resident-only fanout target at dispatch
(`_fanout_dispatch_residency_reason`) now lives in
`sonder_runtime/domain/fanout_residency.py` as `dispatch_residency_reason`,
with the profile check, the residency parsing and both skip reasons
unchanged. The live residency fetch is injected as `fetch_resident`;
`server.py` keeps the root name as a thin delegate passing a `_get("/api/ps")`
closure at call time, so the Ollama transport seam keeps working.

## Evidence

- `tests/test_fanout_residency_boundary.py` verifies that only the no-load profile is fenced, the resident, missing and unverifiable outcomes, and the root delegate's transport seam.
- `python -m pytest -q tests/test_fanout_residency_boundary.py tests/test_model_fanout.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
