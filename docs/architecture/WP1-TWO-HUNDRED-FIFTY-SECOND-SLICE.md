# WP1 Two-Hundred-Fifty-Second Slice — model-error formatting boundary

## Boundary

Moved provider-error rendering and transient HTTP retry-hint formatting from
`server.py` into `sonder_runtime.adapters.model_error_formatting`. The root
continues to classify local/remote/hosted endpoint context and preserves the
historical `_format_model_call_error` wrapper and constants.

## Evidence

- `tests/test_model_error_formatting_adapter.py` covers retry hints and unknown
  failures.
- Existing server, model-retry, and context-overflow regressions preserve the
  historical output contract.
- Focused result: **337 passed**.
- Architecture, compile, and diff checks pass.
