# WP1 One-Hundred-Sixty-Third Slice — Model Request Timeout Policy Ownership

## Boundary

Moved the pure model request-timeout normalization policy from `server.py`
into `sonder_runtime.domain.request_timeout`. The root module retains a small
compatibility wrapper so the process-level `TIMEOUT` ceiling remains live for
existing callers.

## Invariants

- Missing and malformed values still resolve to the configured `TIMEOUT`.
- Request timeouts remain clamped to at least one second.
- Values above the configured ceiling remain capped at that ceiling.
- Ollama transport, retry, and error behavior are unchanged.

## Evidence

- `python -m pytest tests/test_request_timeout_policy.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
