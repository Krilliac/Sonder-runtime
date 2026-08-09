# Tool capability registry (shadow phase)

`tool_capabilities.py` is an immutable, declarative description of a focused
slice of Sonder's tools. It records effect, visibility, permission and root
requirements, network/cloud and secret handling, execution mode, coarse
resource use, and inspection/deduplication behavior.

The `cloud` field governs whether tool-derived data may enter a hosted-model
prompt; it does not describe where the tool implementation executes. The
initial descriptors are deliberately `local-only`, including host inventory,
because repository contents and machine details are private by default.

Hosted-agent eligibility is also captured from the authoritative dispatcher.
Today that surface exposes this initial slice to consented hosted agents, so the
shadow report deliberately returns `ERROR` for each `local-only` descriptor.
This is a visible privacy-policy mismatch, not an enforcement change: callers
must not interpret an `ERROR` report as proof that a tool was blocked.

The registry is intentionally **not authoritative yet**. MCP decorators,
`_agent_dispatch`, help text, and the existing policy sets continue to control
runtime behavior. Importing the registry cannot register, allow, deny, or run a
tool. This avoids turning incomplete coverage into an accidental policy change.

Drift is visible in two places:

- focused tests call `assert_shadow_valid` and fail on a mismatch;
- `diagnostics()` prints `tool capability shadow: ERROR ...` when descriptors
  disagree with the live MCP/help/allow-list/dispatch/inspection surfaces.

The initial slice covers `environment_status`, `hardware_profile`, and common
repository reads. Add tools only after all of their current surfaces and policy
semantics are represented. A later, separately reviewed phase may generate
selected policy surfaces from the registry after coverage is complete.
