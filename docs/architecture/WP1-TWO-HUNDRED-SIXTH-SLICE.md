# WP1 Two-Hundred-Sixth Slice

## Boundary

The standalone client's `SONDER_FALLBACK_LOCAL` environment policy now lives
in `sonder_runtime.platform.client_fallback.enabled`. The packaged platform
boundary owns the default-on and disabled-value semantics, while
`sonder_client.local_fallback_enabled` remains an identity-preserving
compatibility export. Request construction, network transport, and fallback
orchestration remain in the standalone client.

## Evidence

- `tests/test_client_fallback_policy.py` verifies default-on behavior, all
  disabled spellings, permissive enabled values, and the legacy export.
- `python -m pytest -q tests/test_client_fallback_policy.py tests/test_client.py`
  passes.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py sonder_client.py` passes.
- `git diff --check` passes.
