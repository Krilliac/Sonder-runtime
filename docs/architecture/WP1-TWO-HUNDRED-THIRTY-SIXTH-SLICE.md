# WP1 Two-Hundred-Thirty-Sixth Slice — debug-dump logging seam

## Boundary

Migrated the debug-dump export boundary from the compatibility-root
`sonder_logging.Redactor` import to the canonical packaged
`sonder_runtime.platform.logging` seam. Debug dumps still apply the same
configured-value and textual-secret redaction policy before durable export.
The root `sonder_logging` identity and the `debug_dump.Redactor` module alias
remain unchanged for legacy imports and monkeypatches. This slice is limited
to the debug-dump observability boundary; latency formatting from slice 230
and other remaining logging callers are unchanged.

## Evidence

- `tests/test_debug_dump.py` verifies packaged redactor ownership and identity
  preservation, in addition to the existing dump and redaction behavior.
- `python -m pytest tests/test_debug_dump.py tests/test_logging_platform_seam.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime debug_dump.py sonder_logging.py` passes.
- `git diff --check` passes.
