# WP1 Three-Hundred-Forty-Third Slice — agent escalation identity

## Boundary

Moved `_agent_escalation_key` from server.py into
`sonder_runtime/domain/agent_escalation_identity.py` as `escalation_key`.

Identity key for agent model-escalation failure tracking: combines the tier
name (lowered, stripped) with a truncated SHA-256 of the prompt. Pure
function, stdlib only (hashlib).

The root `server._agent_escalation_key` is an identity-preserving alias.

## Evidence

- `tests/test_agent_escalation_identity_boundary.py` verifies identity alias,
  determinism, format (tier:digest), None handling, and prompt differentiation.
- `python -m pytest -q tests/test_agent_escalation_identity_boundary.py` — 5 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/domain/agent_escalation_identity.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
