# WP1 one-hundred-twentieth slice — tool-executor adapter ownership

## Scope

`LegacyToolExecutor` was a generic ownership boundary in
`sonder_runtime.adapters.strangler_services`. Its implementation now lives in
the canonical packaged adapter `sonder_runtime.adapters.tool_executor` as
`ToolExecutorAdapter`. The old name remains an identity-preserving compatibility
alias, while `bootstrap.app` constructs the canonical adapter directly.

The adapter still delegates containment, authorization, and execution policy to
the packaged filesystem/workbench primitives and preserves the existing
deadline, cancellation, result mapping, and fail-closed error behavior. No
`server.py` or `unsafe_lab` files were changed by this slice.

## Verification

- `pytest -q tests/test_legacy_tool_executor.py tests/test_strangler_services_paths.py tests/test_local_observability.py` — 21 passed.
- `python -m compileall -q sonder_runtime/adapters/tool_executor.py sonder_runtime/adapters/strangler_services.py sonder_runtime/bootstrap/app.py` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `git diff --check` — pass.

This slice is intentionally uncommitted and unpushed at the user's request.
