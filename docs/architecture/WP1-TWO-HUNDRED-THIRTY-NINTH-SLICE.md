# WP1 Two-Hundred-Thirty-Ninth Slice — standalone client transport boundary

## Boundary

Moved standalone-client HTTP request execution and chat-response extraction
from root `sonder_client.py` into the packaged
`sonder_runtime.adapters.client_transport` adapter. The root `send_prompt`
callable remains the compatibility surface and forwards the historical
`build_request` seam into the adapter, preserving request construction,
`urllib` exception propagation, response decoding, and payload semantics.
Fallback orchestration, configuration, and request construction remain outside
this slice.

## Evidence

- `tests/test_client_transport_adapter.py` verifies packaged ownership,
  transport request wiring, response extraction, and root request-builder
  compatibility.
- `python -m pytest -q tests/test_client_transport_adapter.py tests/test_client.py tests/test_client_request_adapter.py tests/test_client_fallback_policy.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_client.py`
- `git diff --check`
