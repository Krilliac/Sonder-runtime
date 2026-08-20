# WP1 one-hundred-nineteenth slice — fanout eligibility policy ownership

## Scope

The pure catalog-target eligibility policy previously lived in `server.py`.
This slice moves it to `sonder_runtime.domain.fanout_policy`, preserving the
`server._fanout_nonchat_reason` compatibility alias while keeping the policy
explicit-input, side-effect free, and independent of transport code.

## Verification

- `pytest -q tests/test_fanout_policy.py` — 8 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
