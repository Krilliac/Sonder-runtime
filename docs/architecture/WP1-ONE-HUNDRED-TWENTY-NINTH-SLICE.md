# WP1 one-hundred-twenty-ninth slice — campaign output policy ownership

## Scope

The pure `_campaign_output_matches` policy previously lived in `server.py`.
This slice moves its implementation to
`sonder_runtime.domain.campaign_policy.output_matches`, preserving the
identity-compatible `server._campaign_output_matches` alias. Exact output
matching still normalizes platform line endings, outer blank lines, and
per-line surrounding whitespace while rejecting prose and extra lines.

No adapters or prior migration implementations were changed.

## Verification

- `python -m pytest tests/test_campaign_policy.py tests/test_server_helpers.py -k campaign -q` — passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
