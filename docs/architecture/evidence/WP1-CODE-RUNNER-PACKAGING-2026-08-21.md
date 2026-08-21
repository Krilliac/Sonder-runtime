# WP1 code-runner packaging evidence — 2026-08-21

## Decision

`code_runner.py` is a host-process execution adapter. Its ownership is now
`sonder_runtime.adapters.execution_tools.code_runner`; it does not belong in
the root application namespace. The existing root module remains an identity
compatibility alias so legacy imports and test monkeypatches continue to
target the canonical module.

The former `sonder_runtime.adapters.execution_tools` provider module became a
package. Its `CodeRunnerProvider` and `GroundingProvider` exports remain
available, while the concrete runner is imported directly from the package.
Production callers in `game_forge.py` and `verifiers.py` now use the packaged
adapter. The adapter also uses canonical packaged logging and path modules,
so the architecture checker sees no root-module dependency.

## Verification

- `tests/test_code_runner.py`
- `tests/test_code_runner_realpath.py`
- `tests/test_runner_languages.py`
- `tests/test_unsafe_lab.py`
- `tests/test_execution_tools_provider.py`
- `tests/test_code_runner_packaging.py`
- Result: 122 passed, 9 skipped.
- `python scripts/check_architecture.py`: passed.
- `python -m compileall -q sonder_runtime/adapters/execution_tools code_runner.py game_forge.py verifiers.py`: passed.

## Boundary status

The root alias is intentionally retained as a compatibility boundary and is
ratcheted in `COMPATIBILITY_ROOT_MODULES`. No Git metadata was changed; the
working-tree move remains to be committed by the branch owner.
