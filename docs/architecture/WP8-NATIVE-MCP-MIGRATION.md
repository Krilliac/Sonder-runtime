# Native MCP transport migration slice

Status: implemented on `agent/wp1-execution-status` as an explicit opt-in
surface (`python -m sonder_runtime mcp --native`).

## Scope

The native path composes the bounded JSON-RPC stdio transport with the
application-owned `ToolExecutor` port and a deterministic generated catalog
for the six tools currently owned by `ToolExecutorAdapter`. Each call gets a
fresh MCP `OperationContext`, and executor results retain bounded error and
evidence fields.

The historical server MCP catalog remains the default compatibility path until
catalog parity and complete application-service coverage are proven. This
slice therefore does not claim API-003 or TOOL-001 completion.

## Evidence

- `tests/test_native_mcp.py`: deterministic catalog and end-to-end transport
  to application tool-port translation.
- `tests/test_mcp_stdio_transport.py`: negotiation, bounded frames,
  subscriptions, malformed input, and catalog limits.
- Focused result: **38 passed**.
- `scripts/check_architecture.py`, compileall, and diff checks pass.
