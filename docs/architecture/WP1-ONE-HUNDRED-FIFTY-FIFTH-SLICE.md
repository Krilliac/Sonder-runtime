# WP1 One-Hundred-Fifty-Fifth Slice — Retry-After policy ownership

## Boundary

Moved the pure upstream HTTP `Retry-After` parsing policy out of root
`server.py` and into `sonder_runtime.domain.retry_after`. The server keeps an
identity-compatible `_retry_after_seconds` alias so existing transport and
test callers retain their compatibility surface. The policy still accepts
delta-seconds and HTTP-date values, clamps negative and excessive hints, and
rejects malformed or non-finite input.

## Verification

- `python -m pytest tests/test_retry_after_policy.py tests/test_model_retry.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
