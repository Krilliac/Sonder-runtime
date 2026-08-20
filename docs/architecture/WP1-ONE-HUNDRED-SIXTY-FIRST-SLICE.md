# WP1 One-Hundred-Sixty-First Slice — cancellation policy ownership

## Boundary

Moved the pure cancellation safety gate from root `server.py` into the
canonical `sonder_runtime.domain.cancellation_policy` boundary. The server
retains `_cancel_requested` as an identity-compatible alias, while model
transport and orchestration call sites remain behaviorally unchanged.

## Verification

- `python -m pytest tests/test_cancellation_policy.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
