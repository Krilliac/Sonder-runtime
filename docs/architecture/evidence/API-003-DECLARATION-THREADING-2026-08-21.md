# API-003 declaration threading — 2026-08-21

The composed `LegacyMcpContract` now reaches the in-repo stdio MCP transport's
actual `McpCompatibility.negotiate` call. `StdioMcpTransport` accepts an
optional typed declaration and passes that exact object during initialization;
the transport rejects a declaration that is not already registered with the
same compatibility contract.

Focused evidence in `tests/test_mcp_stdio_transport.py` proves legacy
negotiation succeeds only when the registered composed declaration is threaded,
and that an unregistered or omitted declaration remains fail-closed. Existing
MCP 2.0 negotiation is unchanged. This slice does not claim a separately
deployed subprocess/provider boundary.

```text
python -m pytest -q tests/test_api003_legacy_declaration.py tests/test_mcp_stdio_transport.py tests/test_wp8_mcp_compatibility.py
```
