# API-003 subprocess/provider proof — 2026-08-21

This slice adds an offline proof and local production composition seam for the
remaining subprocess/provider boundary.
`McpSubprocessProvider` owns a one-shot child-process MCP exchange over
stdin/stdout. It bounds the call, delegates tree termination to the existing
process-termination adapter, reaps the child before returning, and emits safe
start/exit/timeout lifecycle observations. The transport also enforces an
explicit bounded request/response exchange size. The child uses the existing typed
`StdioMcpTransport`, negotiates MCP 2.0, lists its typed tools, performs an
`echo` tool call, rejects an oversized argument payload with a bounded protocol
error, and is terminated by production adapter behavior when a provider call
does not return.

The child has no network path or network dependency. The test asserts clean EOF
termination for the normal exchange, lifecycle notification ordering, bounded
timeout cleanup, and child-originated negotiated subscription notification
forwarding. `build_mcp_subprocess_exchange` is the safe local
composition seam: it accepts immutable launch settings, constructs the typed
provider, and returns the bounded provider exchange without exposing process
ownership to application callers. Invalid argv, environment, and timeout
settings fail before launch. This remains local/offline evidence only; API-003
is still partial because separately deployed provider acceptance, including its
external process packaging and interoperability matrix, is not available in
this repository. The master requirement/checklist was not edited.

```text
python -m pytest -q tests/test_api003_subprocess_provider.py tests/test_mcp_stdio_transport.py
```
