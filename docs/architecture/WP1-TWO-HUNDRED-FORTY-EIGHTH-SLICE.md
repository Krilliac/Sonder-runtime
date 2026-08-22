# WP1 Two-Hundred-Forty-Eighth Slice — standalone client fallback boundary

## Boundary

Moved standalone-client endpoint normalization/comparison and connection-error
fallback orchestration into the packaged `sonder_runtime.adapters` client
boundaries. `sonder_client.py` remains the compatibility/composition surface:
`LOCAL_FALLBACK_SERVER`, `_same_server`, and
`send_prompt_with_fallback` preserve their historical names and behavior,
including injectable request sending for callers and tests.

The existing client configuration, request-construction, and HTTP transport
adapters are unchanged and deliberately outside this slice.

## Evidence

- `tests/test_client_fallback_policy.py` verifies packaged endpoint ownership,
  endpoint normalization, and local fallback endpoint resolution.
- `tests/test_client.py` verifies packaged fallback ownership and existing
  hosted/local fallback behavior.
- `python -m pytest -q tests/test_client.py tests/test_client_fallback_policy.py tests/test_client_request_adapter.py tests/test_client_transport_adapter.py tests/test_client_config_adapter.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_client.py`
- `git diff --check`
