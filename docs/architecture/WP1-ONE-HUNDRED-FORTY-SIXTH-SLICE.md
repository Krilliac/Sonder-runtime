# WP1 One-Hundred-Forty-Sixth Slice

## Boundary

Moved process-wide application-instance lifecycle ownership from the
composition root implementation into the canonical packaged
`sonder_runtime.adapters.application_lifecycle.ApplicationLifecycle` adapter.
Bootstrap remains the compatibility-facing composition root and continues to
expose `default_app()` and `reset_for_tests()` with the same lazy, atomic,
and reset semantics.

## Verification

- `tests/test_application_lifecycle_adapter.py`: 3 passed.
- Existing composition-root atomicity and lifecycle regressions passed.
- Architecture, requirement-evidence, compile, and `git diff --check` gates passed.

The adapter owns only generic lifecycle synchronization; application graph
construction remains in bootstrap and no server or transport implementation
was moved in this slice.
