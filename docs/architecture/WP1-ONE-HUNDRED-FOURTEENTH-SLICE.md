# WP1 one-hundred-fourteenth slice — HTTP serve policy ownership

## Scope

The HTTP serving interface previously owned the environment-backed
`server._serve_temperature` policy. This slice moves that pure policy to
`sonder_runtime.interfaces.http.serve_policy`, while retaining the server
helper as a compatibility alias for existing callers and monkeypatches.

## Verification

- `pytest -q tests/test_http_serve_policy.py` — 10 passed.
- `python -m compileall -q sonder_runtime/interfaces/http/serve_policy.py server.py` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
