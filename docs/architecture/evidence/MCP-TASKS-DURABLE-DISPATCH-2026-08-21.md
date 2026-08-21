# MCP Tasks durable dispatch seam — 2026-08-21

`McpTaskHandler` now adapts the durable application job service to the
negotiated `tasks/get`, `tasks/update`, and `tasks/cancel` methods. It projects
only `McpTaskView` metadata, keeps result and error content redacted, routes
cancellation through the job service, and validates task identifiers, reasons,
poll bounds, and update metadata before the transport sees the response.

The handler is provider-neutral and can be injected directly into
`StdioMcpTransport`; the transport still requires the client to negotiate the
`tasks` capability before dispatch. Focused proof covers direct get/update/
cancel behavior and the real negotiated stdio path:

```text
python -m pytest -q tests/test_mcp_task_handler.py tests/test_mcp_tasks_projection.py tests/test_mcp_stdio_transport.py
```

This slice does not claim that every native MCP tool starts a durable task, nor
does it expose task result content. The existing API-003 subprocess and
restart-recovery limitations remain unchanged.
