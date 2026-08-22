# SEAM-002 typed tool caller — 2026-08-21

## Scope

This bounded slice routes the existing application `ToolGateway` schema and
invocation collaborators through the typed `ToolRegistry` and
`ToolExecutor` ports.  It does not alter MCP, HTTP, filesystem, process,
session, job, loop, child-provider, memory, training, data, evaluation,
update, operations, model, compaction, or selfmod code.

## Implemented boundary

- `RegistrySchemaValidator` performs lookup and `validate_tool_call` against
  the immutable application registry.
- `PortBackedToolInvoker` creates an explicit request context, invokes the
  typed policy, selects the execution class, and calls the typed executor.
- `ToolGateway.from_typed_ports` composes those adapters without changing the
  existing gateway contract.
- Scope and permission evaluation, approval, deadline/cancellation checks,
  output redaction, optional durable audit, and receipt publication remain in
  `ToolGateway`; they are not bypassed by the migration.

## Evidence

```text
python -m pytest -q tests/test_seam002_typed_gateway.py tests/test_crosscutting_tool_gateway.py
python -m compileall -q sonder_runtime/application/tools/typed_gateway.py sonder_runtime/application/tools/gateway_contract.py
python scripts/check_architecture.py
python scripts/check_evidence_documents.py
git diff --check
```

The focused tests assert pipeline ordering, principal/scope propagation,
schema rejection before policy/executor dispatch, approval rejection,
deadline and cancellation fail-closed behavior, redaction, and receipts.
