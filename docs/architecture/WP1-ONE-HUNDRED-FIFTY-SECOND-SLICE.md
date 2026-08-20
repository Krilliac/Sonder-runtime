# WP1 One-Hundred-Fifty-Second Slice

## Boundary

Moved the stateful `LegacyTaskRepository` implementation out of the generic
`task_store` module into the canonical packaged `TaskRepositoryAdapter`.
`task_store.LegacyTaskRepository` remains an identity-preserving compatibility
alias. The checklist event sink in `task_store` was intentionally left
unchanged because it is a separate event boundary.

## Evidence

- `tests/test_task_repository_adapter.py` verifies canonical ownership,
  delegation, compatibility identity, and persistence-error translation.
- `tests/test_task_application_service.py` continues exercising the existing
  service behavior through the compatibility import.
- Architecture, requirement-evidence, compile, and diff gates pass for this
  working-tree slice.

## Scope exclusions honored

This slice does not modify `server.py`, workflow adapters, or any previously
migrated repository, tool, event, gateway, UnitOfWork, preference, evaluation,
inspection, capability, CLI, configuration, lifecycle, runtime-container, or
environment boundary.

