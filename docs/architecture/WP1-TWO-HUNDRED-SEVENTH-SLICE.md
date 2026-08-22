# WP1 Two-Hundred-Seventh Slice — model transport error-detail adapter

## Boundary

Moved HTTP-body extraction and non-HTTP reason formatting for model transport
errors out of root `server.py` into the packaged
`sonder_runtime.adapters.model_error_details` adapter. The root helpers remain
compatibility delegates, while credential redaction and bounded formatting
remain owned by the existing domain model-error policy.

This slice is limited to the two adjacent transport-detail helpers used by the
model request path; it does not alter the model retry, embedded-error, or
general model-error-formatting boundaries migrated earlier.

## Verification

- `python -m pytest tests/test_model_error_details_adapter.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
