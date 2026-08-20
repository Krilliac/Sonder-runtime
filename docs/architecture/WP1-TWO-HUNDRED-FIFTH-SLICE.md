# WP1 Two-Hundred-Fifth Slice — in-band model-error ownership

## Boundary

Moved the pure extraction and safe formatting of an in-band model response
error from root `server.py` into the existing domain model-error formatting
boundary. `server.py` retains `_embedded_model_error` as a compatibility
wrapper for existing callers.

## Invariants

- Non-mapping responses and responses without a truthy `error` remain empty.
- Scalar error values use the existing credential-redaction policy.
- Structured error values retain deterministic redaction and serialization.
- HTTP body reading, transport handling, retry behavior, and request flow stay
  in their existing boundaries.

## Verification

- `python -m pytest tests/test_embedded_model_error.py tests/test_model_error_formatting.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
