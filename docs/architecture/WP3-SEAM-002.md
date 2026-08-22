# WP3 SEAM-002 — ToolRegistry / ToolExecutor contract

Status: contract modules and tests added; no adapter migration is included.

## Boundary

Model-authored calls cross the application boundary in this order:

```text
ToolRegistry lookup → descriptor validation → ToolPolicy authorization
  → execution-class selection → ToolExecutor adapter → ToolExecutionResult
```

`ToolDescriptor` is immutable metadata: name, description, object-shaped input
schema, declared effects, and the requested execution class. `ToolCall` carries
the selected name, arguments, and optional call id. The registry is responsible
for lookup and registration uniqueness; it does not execute tools.

`validate_tool_call` performs deterministic, side-effect-free validation of the
contract's JSON-Schema-shaped input (`object`, required/properties,
additionalProperties, scalar types, enum/const, arrays, and string bounds).
Malformed or mismatched calls raise the application `InvalidInput` error.

`ToolPolicy` is an explicit authorization boundary. It may reject a call and
may downgrade or select an execution class. The executor receives only after
that decision and must return `ToolExecutionResult`; it is not an authority
source and must not infer permissions from user arguments.

## Scope and non-goals

This slice adds `application/ports/tool_registry.py` and
`application/ports/tool_execution.py`, plus focused contract tests. Existing
executor modules and composition wiring are intentionally unchanged. Concrete
filesystem, process, container, network, timeout, cancellation, receipt, and
approval behavior remain adapter/application follow-up work.

## Verification

```text
pytest -q tests/test_wp3_seam002_tool_contract.py
python -m compileall -q sonder_runtime/application/ports/tool_registry.py sonder_runtime/application/ports/tool_execution.py
```
