# WP1 one-hundred-thirty-third slice — workflow repository ownership

## Scope

The saved-workflow repository implementation previously lived in the generic
workflow adapter module. This slice moves it to the canonical
`sonder_runtime.adapters.workflow_repository.WorkflowRepositoryAdapter`,
rewires bootstrap to compose that adapter directly, and preserves the legacy
identity alias for existing callers.

## Verification

- Focused workflow repository tests — 27 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
