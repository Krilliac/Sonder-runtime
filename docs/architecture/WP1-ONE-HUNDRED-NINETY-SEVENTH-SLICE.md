# WP1 One-Hundred-Ninety-Seventh Slice — permission-mode context ownership

## Boundary

Moved the pure permission-mode context rendering policy out of root
`server.py` into `sonder_runtime.domain.permission_context`. The root server
retains the compatibility helper and continues to own stateful reads of the
active permission mode and elevation status. The extracted domain boundary
accepts those values as inputs and performs no I/O or mutation.

## Evidence

- `tests/test_permission_context.py` verifies label/blurb rendering, unknown
  mode fallback, and multiline elevation text preservation.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.

## Scope

This slice changes only permission-mode context formatting and its root
compatibility wrapper. Permission-rule loading, permission-mode state, and
elevation state remain outside the pure domain module.
