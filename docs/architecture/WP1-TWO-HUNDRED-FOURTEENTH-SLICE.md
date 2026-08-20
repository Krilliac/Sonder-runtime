# WP1 Two-Hundred-Fourteenth Slice — standalone client request ownership

## Boundary

Moved standalone-client chat request construction from the root
`sonder_client.py` script into the packaged `sonder_runtime.adapters` boundary.
The root script retains `build_request` as a compatibility delegate, while
network execution remains in the standalone client. This slice does not modify
`server.py` or the adjacent slice-213 boundary.

## Evidence

- `tests/test_client_request_adapter.py` verifies packaged ownership, the
  compatibility delegate, URL normalization, authentication headers, and the
  JSON request contract.
- `python -m pytest tests/test_client_request_adapter.py tests/test_client.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_client.py` passes.
- `git diff --check` passes.
