# WP1 one-hundred-thirtieth slice — preference codec ownership

## Scope

The preference codec implementation previously lived in the generic
`preference_adapters` module. This slice moves the codec implementation to
`sonder_runtime.adapters.preference_codec.PreferenceCodecAdapter`, rewires
bootstrap to compose it directly, and preserves the legacy codec identity
alias for existing callers.

## Verification

- Focused preference and architecture tests — 69 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
