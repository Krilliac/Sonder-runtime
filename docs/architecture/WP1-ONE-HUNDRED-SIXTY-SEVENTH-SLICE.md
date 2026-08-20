# WP1 One-Hundred-Sixty-Seventh Slice: Control History Policy

## Boundary

Moved the pure interactive control-history message normalization policy out of
root `server.py` into the `sonder_runtime.domain.control_history` boundary.
The root helper remains an identity-preserving compatibility alias. This slice
does not change control-command timeout parsing, runnable-block extraction,
project-file extraction, or any earlier migration boundary.

## Evidence

- `tests/test_control_history.py` verifies filtering, prompt ordering, malformed
  input handling, and the root compatibility alias.
- `python -m pytest tests/test_control_history.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
