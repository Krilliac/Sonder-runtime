# Cross-cutting ToolService gateway contract (TOOL-001–005)

## Scope

This slice establishes the provider-neutral application contract for one
ToolService invocation. It is intentionally isolated from registries,
providers, transports, persistence, subprocesses, and network I/O.

## Pipeline

`ToolGatewayRequest` carries the immutable principal/workspace/effect scope,
permission and approval mode, arguments, deadline, cancellation signal, and
request identity. `ToolGateway.execute` enforces the following order:

1. deadline and cancellation preflight;
2. typed schema validation;
3. permission evaluation against the supplied scope;
4. explicit approval when required;
5. deadline and cancellation recheck;
6. invocation through the `ToolInvoker` port;
7. output redaction;
8. receipt publication.

Receipts contain only redacted output and stable status fields. Provider
adapters implement ports outside this seam; this module performs no provider
I/O and cannot widen scope or bypass approval.

## Evidence

`tests/test_crosscutting_tool_gateway.py` verifies pipeline ordering,
approval denial, pre-provider deadline/cancellation rejection, typed scope and
permission validation, redaction-before-receipt, and receipt publication.

Formal specification checkboxes are intentionally unchanged.
