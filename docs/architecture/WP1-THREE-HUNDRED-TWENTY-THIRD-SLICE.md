# WP1 Three-Hundred-Twenty-Third Slice — agent decision generation

## Boundary

The generation of one structurally valid agent decision with bounded
format repair (`_agent_generate_decision`) and its repair limit now live in
`sonder_runtime/adapters/agent_decision_generation.py` as `generate_decision`
and `DECISION_REPAIR_LIMIT`, with the repair prompts, the length-recovery
instruction, the transport-failure outcome and the cancellation pass-through
unchanged. It catches the transport's `ModelCallError`, which is defined in
the adapters layer, so that layer is its home; it imports the packaged
decision parser directly. The length-recovery chunk budget is injected as
`write_chunk_hint` because the hosted agent budgets stay with the agent
loop. `server.py` keeps `_AGENT_DECISION_REPAIR_LIMIT` as an
identity-preserving alias and `_agent_generate_decision` as a thin delegate
injecting `_CLOUD_AGENT_WRITE_CHUNK_HINT` at call time.

## Evidence

- `tests/test_agent_decision_generation_boundary.py` verifies the constant alias, an unrepaired valid decision, bounded repair with the host prompts, exhaustion, transport failure and cancellation handling, the length-recovery hint with an injected budget, and the root delegate's server budget.
- `python -m pytest -q tests/test_agent_decision_generation_boundary.py tests/test_agent_tools.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
