# WP1 one-hundred-twenty-eighth slice — health-meter formatting ownership

## Scope

The pure percentage-to-meter formatter previously lived in `server.py`.
This slice moves it to `sonder_runtime.domain.health_formatting`, preserving
the `server._health_bar` compatibility alias and its clamping, rounding, and
fixed-width rendering contract.

## Verification

- `pytest -q tests/test_health_formatting.py` — 7 passed.
- `pytest -q tests/test_server_helpers.py -k context_health` — 2 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest runs emitted only the known non-fatal Windows pytest-cache
permission warning.
