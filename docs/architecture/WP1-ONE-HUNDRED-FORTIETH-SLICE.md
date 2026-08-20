# WP1 one-hundred-fortieth slice — learning-tier policy ownership

## Scope

The pure `_canonical_learn_tier` policy previously lived in `server.py`.
This slice moves it to `sonder_runtime.domain.learning_tier`, preserving the
identity-compatible `server._canonical_learn_tier` alias and its historical
normalization contract.

## Verification

- Focused learning-tier tests — 3 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
