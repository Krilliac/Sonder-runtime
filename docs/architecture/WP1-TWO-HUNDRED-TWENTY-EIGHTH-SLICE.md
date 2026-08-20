# WP1 Two-Hundred-Twenty-Eighth Slice — launcher health status ownership

## Boundary

Moved the pure launcher-health nonce, identity, canonical-message, and HMAC
proof policy from root `sonder_health.py` into the packaged
`sonder_runtime.domain.launcher_health` boundary. The root module remains an
identity-preserving compatibility surface for its constants and helper names,
including the private identity validator used by legacy tests and callers.

The existing `sonder_runtime.domain.health_formatting` health-meter boundary
is unchanged. Launcher orchestration, HTTP serving, `system_profile`, and
`doctor` are outside this slice.

## Verification

- `pytest -q tests/test_health_status.py tests/test_health_formatting.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_health.py`
- `git diff --check`
