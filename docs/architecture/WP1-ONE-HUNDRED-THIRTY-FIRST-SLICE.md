# WP1 one-hundred-thirty-first slice — model usage policy ownership

## Scope

The pure `_model_usage_count` normalization policy previously lived in the
monolithic `server.py` entry module. This slice moves its implementation to
`sonder_runtime.domain.model_usage.usage_count` and preserves the server
symbol as an identity-compatible alias for existing callers.

The policy still converts provider values to non-negative integers and returns
`None` for missing, malformed, overflowing, or negative values. No transport,
storage, adapter, or prior migration behavior changed.

## Verification

- Focused model-usage tests — 9 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime` — pass.
- `git diff --check` — pass.

The focused pytest run may emit the known non-fatal Windows `.pytest_cache`
access warning.
