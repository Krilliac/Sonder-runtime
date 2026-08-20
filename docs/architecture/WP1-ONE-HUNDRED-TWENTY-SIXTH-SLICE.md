# WP1 one-hundred-twenty-sixth slice — model routing policy ownership

## Scope

The pure lexical cloud-model-name classifier previously lived in `server.py`.
This slice moves it to `sonder_runtime.domain.model_routing`, preserving the
`server._is_cloud_model_name` compatibility alias and its historical matching
contract.

## Verification

- `pytest -q tests/test_model_routing.py` — passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
