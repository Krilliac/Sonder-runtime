# WP1 One-Hundred-Fifty-Ninth Slice — model overflow retry policy ownership

## Boundary

Moved the environment-backed hosted/remote model overflow-retry eligibility
policy from the root `server.py` implementation into
`sonder_runtime.platform.model_retry_policy`.
The server retains compatibility aliases, while request transport and retry
execution remain in the server boundary.

## Verification

- `python -m pytest tests/test_model_retry_policy_ownership.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
