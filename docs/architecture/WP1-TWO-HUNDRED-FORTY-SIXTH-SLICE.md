# WP1 Two-Hundred-Forty-Sixth Slice — packaged doctor configuration check

## Boundary

Moved the remaining read-only configuration-check policy from root
`sonder_doctor.py` into `sonder_runtime.bootstrap.config_loading`. The packaged
bootstrap boundary now owns loading, `ConfigError` diagnostics, and the
unavailable-config fallback; typed parsing remains owned by
`sonder_runtime.platform.config`, and the validated-result projection remains
owned by `sonder_runtime.adapters.config_validation`.

The root `_check_config` helper remains as a compatibility delegate. Doctor
formatting, status coercion, and rollup behavior are unchanged.

## Evidence

- `tests/test_bootstrap_config_loading.py` covers successful validation,
  configuration errors, and the root compatibility delegate.
- Focused bootstrap/doctor tests, architecture checks, requirement-evidence
  checks, compilation, and diff whitespace checks pass.
