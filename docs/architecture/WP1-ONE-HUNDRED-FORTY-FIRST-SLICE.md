# WP1 one-hundred-forty-first slice — context token-estimate ownership

## Scope

The pure `_rough_token_count` helper now lives in
`sonder_runtime.domain.context_formatting.rough_token_count`. The server keeps
an identity-compatible import alias, so existing callers retain the same
callable and output contract.

The packaged domain helper accepts only explicit input, performs no I/O, and
retains the historical empty-value behavior and four-characters-per-token
estimate used by context-health and model-usage reporting.

## Verification

- Focused tests: `python -m pytest tests/test_context_formatting.py -q`
- Architecture gate: `python scripts/check_architecture.py`
- Compile gate: `python -m compileall -q server.py sonder_runtime tests/test_context_formatting.py`
- Requirement-evidence gate: `python scripts/check_requirement_evidence.py`
- Diff gate: `git diff --check`
