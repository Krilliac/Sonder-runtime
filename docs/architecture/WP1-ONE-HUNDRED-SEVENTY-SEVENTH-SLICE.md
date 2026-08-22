# WP1 One-Hundred-Seventy-Seventh Slice: Deployment Authentication Policy Boundary

## Boundary

Moved the pure deployment-authentication policy out of root `server.py` into
`sonder_runtime.platform.deployment_auth`. The root helper remains as a thin
compatibility delegate for direct-MCP and HTTP serving callers; environment
semantics and the local-open default now have one canonical owner.

## Evidence

- `tests/test_deployment_auth_policy.py` verifies local-open behavior, auth-mode
  and API-key detection, accepted account flags, and invalid/blank values.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
