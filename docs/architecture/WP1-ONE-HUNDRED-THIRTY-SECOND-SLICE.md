# WP1 one-hundred-thirty-second slice — workflow loop-runner ownership

## Scope

The workflow `LegacyLoopRunner` implementation previously lived in the
generic workflow adapter module. This slice moves it to the canonical
`sonder_runtime.adapters.workflow_loop_runner.LoopRunnerAdapter`, rewires
bootstrap to compose that adapter directly, and preserves the legacy identity
alias for existing callers.

## Verification

- Focused workflow loop-runner tests — 10 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
