# API-003 bounded subprocess/provider lifecycle — 2026-08-21

`McpSubprocessProvider` now carries an explicit typed provider declaration,
launches a one-shot process over bounded stdio, and supports a monotonic
deadline plus cooperative cancellation. It records a typed
`ProcessTreeCleanupReceipt`; an unproven descendant cleanup remains
`complete=false`, even when a direct-child safety kill is performed.

Focused proof launches a separate provider artifact from
`tests/fixtures/api003_provider.py` and covers MCP negotiation and tool exchange, frame/exchange bounds,
cancellation, timeout, declaration threading, incomplete-cleanup truth, and a
fresh provider instance after a bounded timeout. HTTP, MCP, and REPL callers
are not widened. This remains local repository evidence only: separately
packaged/deployed release receipts, durable cross-process restart
reconciliation, descendant inventory, and external third-party MCP
interoperability remain unverified.

The installed official MCP Python SDK now also negotiates, lists tools, and
calls `echo` against the same fixture in
`tests/test_api003_official_mcp_sdk.py`. This is third-party client
interoperability evidence for the local fixture only; it does not prove a
separately deployed provider, production restart reconciliation, or full
external ecosystem compatibility.

```text
python -m pytest -q tests/test_api003_subprocess_provider.py tests/test_api003_legacy_declaration.py tests/test_mcp_stdio_transport.py tests/test_process_termination_adapter.py
```

Result: 25 bounded lifecycle tests plus 1 official-SDK interoperability test
passed on Windows 11 / Python 3.12.
