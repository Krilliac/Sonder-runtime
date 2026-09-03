# WP1 Three-Hundred-Thirtieth Slice — hosted availability fallback

## Boundary

The documented K3-to-K2.7 availability fallback
(`_cloud_extra_usage_fallback`, `_chat_request_with_cloud_fallback`) now lives
in `sonder_runtime/adapters/cloud_fallback.py` as `extra_usage_fallback` and
`chat_request_with_cloud_fallback`, with the 402-only rule, the immutable
fanout-row exemption and the fallback payload preparation unchanged. It reads
the transport's `ModelCallError`, which is defined in the adapters layer, so
that layer is its home. The transport call, the hosted thinking policy and
the configured fallback model are injected; `server.py` keeps both root names
as thin delegates passing `_chat_request`, `_apply_cloud_thinking_policy` and
`CLOUD_EXTRA_USAGE_FALLBACK_MODEL` at call time, so the transport monkeypatch
seam keeps working.

## Evidence

- `tests/test_cloud_fallback_boundary.py` verifies the 402-only fallback rule, one fallback with the thinking policy reapplied and the transport arguments preserved, the immutable-target and other-failure pass-throughs, and the root delegate's transport seam.
- `python -m pytest -q tests/test_cloud_fallback_boundary.py tests/test_context_overflow.py tests/test_server_helpers.py -k 'boundary or fallback or cloud'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
