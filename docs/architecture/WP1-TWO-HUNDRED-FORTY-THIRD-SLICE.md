# WP1 Two-Hundred-Forty-Third Slice — speculation configuration boundary

## Boundary

Moved the environment-backed speculative-execution helpers
(`min_saving_seconds`, `speculation_slots`, and `predictor_path`) from the
root `sonder_speculation` implementation into the packaged
`sonder_runtime.platform.speculation` boundary. The root names remain
identity-preserving aliases for external tooling and existing callers.

The closed read-only tool allowlist remains owned by the packaged domain
`sonder_runtime.domain.speculation_policy`; the predictor continues to ask
that policy before issuing a speculative call. This slice does not change
predictor classes, prediction behavior, slot limits, environment names, or
predictor-state path resolution.

## Evidence

- `tests/test_speculation_configuration_boundary.py`
- `tests/test_speculation_policy.py`
- `python -m pytest -q tests/test_speculation_configuration_boundary.py tests/test_speculation_policy.py tests/test_speculation.py tests/test_speculation_multislot.py`
- `python -m compileall -q sonder_runtime sonder_speculation.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --check`
