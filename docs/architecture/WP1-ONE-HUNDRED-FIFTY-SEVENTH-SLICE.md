# WP1 One-Hundred-Fifty-Seventh Slice — model error formatting ownership

## Boundary

Moved the pure model-error redaction and bounded-detail policy from the root
`server.py` module into `sonder_runtime.domain.model_error_formatting`.
`server.py` retains identity-compatible aliases for existing callers. HTTP
response-body reading remains in the server transport boundary.

## Invariants

- Credential-shaped dictionary keys and scalar error values remain redacted.
- Context-related terms such as `token limit` remain readable for overflow
  classification and diagnostics.
- Structured details remain deterministic and bounded to the existing limit.
- No HTTP, retry, or model-request behavior changes.

## Verification

- `python -m pytest tests/test_model_error_formatting.py tests/test_context_overflow.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
