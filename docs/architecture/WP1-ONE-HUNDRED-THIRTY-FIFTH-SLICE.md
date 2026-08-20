# WP1 one-hundred-thirty-fifth slice — workflow repository ownership

## Scope

The saved-workflow repository implementation previously lived in the generic
`workflow_adapters.py` compatibility module. This slice moves its ownership to
`sonder_runtime.adapters.workflow_repository.WorkflowRepositoryAdapter`, wires
bootstrap to the canonical adapter, and preserves the
`LegacyWorkflowRepository` identity alias for existing callers.

The packaged workflow store, repository port, loop-runner adapter, workflow
service behavior, and saved-workflow wire format remain unchanged.

## Verification

- Focused workflow repository, loop-runner, use-case, and store tests — 27 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime` — pass.
- `git diff --check` — pass.

The focused pytest run may emit the known non-fatal Windows `.pytest_cache`
access warning.
