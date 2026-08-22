# WP1 one-hundred-thirty-fifth slice — campaign expected-output ownership

## Scope

The pure `_campaign_expected` task-verdict policy previously lived in
`server.py`. This slice moves its implementation to the canonical domain
module `sonder_runtime.domain.campaign_expectations`, while preserving the
identity-compatible `server._campaign_expected` alias used by existing
callers and tests. The task verdict strings are unchanged.

## Verification

- `python -m pytest -q tests/test_campaign_expectations.py tests/test_server_helpers.py -k campaign` — passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run may emit the known non-fatal Windows pytest-cache
permission warning.
