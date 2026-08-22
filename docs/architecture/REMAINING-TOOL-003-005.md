# REMAINING-TOOL-003-005 — Resource policy and startup authorities

## Contract

`ResourcePolicy` is the provider-neutral decision boundary beneath the existing
ToolService gateway. Rules can constrain tool, operation, path, host, resource,
agent preset, workspace, origin, side-effect class, persistence, and secret
exposure. Matching is fail-closed: an unmatched request is denied, path rules
respect directory boundaries, and wildcard host rules do not match the apex.

`Decision` distinguishes `allow`, `ask`, and `deny` truthfully and also models
allow-once, session/project grants, sandbox-only, and attended-only outcomes.
The result includes an immutable receipt with the matched rule, normalized
resource facts, decision, approval requirement, and startup-authority digest.

`StartupAuthoritySnapshot` is captured at bootstrap and contains independent
`unrestricted_tools` and `unrestricted_selfmod` flags. It is frozen, hashed,
and has no runtime mutation API. A rule requiring an authority is denied when
that authority was not captured at startup; enabling one authority never
enables the other.

## Evidence

`tests/test_remaining_tool_policy.py` covers path and host boundaries, all
resource dimensions, truthful allow/ask/deny decisions and receipts, and
independent immutable startup authorities. The module performs no provider,
network, filesystem, process, or persistence I/O. Formal checklist checkboxes
remain intentionally unchanged.
