# WP1 Three-Hundred-Sixth Slice — agent evidence quality and verifier reach

## Boundary

Two responsibilities left `server.py` into two packaged modules.
`sonder_runtime/domain/agents/evidence_quality.py` holds
`codegen_build_succeeded` (the host-rendered terminal verdict of the codegen
loop) and `tool_observation_ok` (the tool-contract-specific evidence checks
for `web_fetch`, `archive_list` and the codegen loop), the latter taking the
generic success predicate as an injected `observation_ok` callable.
`sonder_runtime/domain/agents/verification_reach.py` holds
`VERIFICATION_TOOLS` and `verifier_reachable`, which classifies a lane by the
two gates the dispatcher enforces and takes the read-only allow-list as an
injected `read_only_tools` keyword. Every predicate and set is unchanged.

`server.py` keeps `_ensemble_codegen_build_succeeded` and
`_AGENT_VERIFICATION_TOOLS` as identity-preserving aliases and keeps
`_agent_tool_observation_ok` and `_agent_verifier_reachable` as thin
delegates injecting `_agent_observation_ok` and `REPOSITORY_READ_ONLY_TOOLS`
at call time. `_agent_observation_ok` deliberately did not move: its
`startswith("ERROR:")` parse is recorded in the shrink-only error-signal
baseline under its current scope.

## Evidence

- `tests/test_agent_evidence_quality_boundary.py` verifies the alias identities and the constant, the codegen terminal verdict, the tool-contract checks through an injected predicate, the root delegate's use of the server predicate, and verifier reach under the read-only and allow-list gates.
- `python -m pytest -q tests/test_agent_evidence_quality_boundary.py tests/test_agent_verification_gate.py tests/test_agent_tools.py tests/test_natural_ensemble_compiler.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
