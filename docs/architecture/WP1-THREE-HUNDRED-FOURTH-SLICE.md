# WP1 Three-Hundred-Fourth Slice — agent tool naming

## Boundary

The alias table that maps short agent tool spellings to registered tool
names (`_AGENT_TOOL_ALIASES`) and the canonicalizer that applies it
(`_canonical_agent_tool_name`) now live in
`sonder_runtime/domain/agents/tool_naming.py` as `AGENT_TOOL_ALIASES` and
`canonical_agent_tool_name`, unchanged. `server.py` keeps both root names as
identity-preserving alias imports, so the agent dispatcher, the HTTP serve
layer, `tool_contract` and `eval_harness` (which all reach the canonicalizer
through the root name) call the same objects.

## Evidence

- `tests/test_agent_tool_naming_boundary.py` verifies the alias identities, alias resolution, pass-through of unknown and empty names, and that every alias target is itself canonical.
- `python -m pytest -q tests/test_agent_tool_naming_boundary.py tests/test_advertised_surface_drift.py tests/test_permission_gate_coverage.py tests/test_permission_gate_dispatch.py tests/test_tool_contract_conformance.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
