# WP1 Two-Hundred-Sixteenth Slice

## Boundary

Moved child-process environment secret/control-name classification from the
structured logging module into the dedicated platform policy module
`sonder_runtime.platform.child_environment_policy`.

`sonder_runtime.platform.logging._unsafe_child_secret_name` remains a
compatibility alias, so existing child-environment behavior and callers are
unchanged while ownership is explicit and independently testable.

## Evidence

- `tests/test_child_environment_policy.py` verifies the new owner, the
  compatibility identity, and representative secret/control and safe names.
- `tests/test_unsafe_lab.py` continues to cover end-to-end child-environment
  scrubbing behavior.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
