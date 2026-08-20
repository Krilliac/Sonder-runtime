# WP1 one-hundred-thirty-fourth slice — campaign headline ownership

## Scope

The pure `_campaign_headline` formatter previously lived in `server.py`.
This slice moves its implementation to the canonical domain module
`sonder_runtime.domain.campaign_formatting`, while preserving the
identity-compatible `server._campaign_headline` alias used by existing
callers and tests. The healthy headline remains byte-identical and non-zero
pitfall failures remain visible on the durable first line.

## Verification

- `python -m pytest -q tests/test_campaign_formatting.py tests/test_repo_repair.py` — 22 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
