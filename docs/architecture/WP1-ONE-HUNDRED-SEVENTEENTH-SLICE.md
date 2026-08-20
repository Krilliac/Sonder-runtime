# WP1 One-Hundred-Seventeenth Slice

## Boundary moved

`LegacyPolicyRepository` no longer lives in the generic
`sonder_runtime.adapters.strangler_services` collection.  Its implementation
now lives in the canonical `sonder_runtime.adapters.runtime_policy_repository`
module as `RuntimePolicyRepository`; both the composition root and
`LegacyUnitOfWork` construct that named adapter directly.

## Behavior preserved

- `load()` still delegates to `runtime_policy.load()`.
- `update()` forwards the same policy fields, source, and optimistic-revision
  guard to `runtime_policy.update()`.
- The application `RuntimePolicyService` and unit-of-work policy port retain
  the same behavior and object contract.
- No server, model, or policy persistence semantics were changed.

## Verification

- `tests/test_runtime_policy_repository.py`: 3 passed.
- Compile gate: passed.
- Architecture gate: passed with zero violations.
- Requirement-evidence gate: passed.
- `git diff --check`: passed.

This is an implementation slice, not a master-spec checkbox credit.
