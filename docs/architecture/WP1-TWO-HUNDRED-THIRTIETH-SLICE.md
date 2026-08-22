# WP1 Two-Hundred-Thirtieth Slice — observability latency helper ownership

## Boundary

Moved the pure percentile helper used by bounded local observability latency
summaries from `sonder_runtime.adapters.local_observability` into the
packaged `sonder_runtime.adapters.observability.latency_formatting` boundary.
`local_observability._percentile` remains an identity-preserving compatibility
alias. The root `sonder_logging` module and its canonical
`sonder_runtime.platform.logging` identity remain unchanged, including the
legacy redaction and logger monkeypatch surfaces. Health and path boundaries
are out of scope.

## Evidence

- `tests/test_latency_formatting.py` verifies packaged ownership, alias
  identity, nearest-rank behavior, and non-mutating input handling.
- Existing logging seam tests continue to verify the root
  `sonder_logging`/packaged logging identity and redaction behavior.
- `python -m pytest tests/test_latency_formatting.py tests/test_logging_platform_seam.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_logging.py` passes.
- `git diff --check` passes.
