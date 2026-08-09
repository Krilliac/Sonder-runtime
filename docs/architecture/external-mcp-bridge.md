# Guarded external MCP bridge

`sonder_runtime.application.external_mcp` defines the host-owned policy and
call boundary for future external MCP integrations. It is intentionally not a
general HTTP client and is not exposed to agents or Sonder's MCP server.

The bridge requires explicit server and tool allowlists, per-tool capability
declarations, host-resolved credential references, bounded arguments/results
and timeouts, and local audit receipts. Read-only capability is the default.
Public HTTPS endpoints additionally require both server policy and the existing
operation-context cloud consent. Loopback endpoints remain separate from public
remote endpoints.

Inline credentials, arbitrary headers, discovery, redirects, private/link-local
destinations, and unrestricted networking are rejected. A future concrete
transport adapter must reconnect for every call, dial only the pinned resolved
addresses, preserve the configured hostname for TLS verification, reject
redirects, enforce the streaming byte limit, and close the session after the
result. Until that separately reviewed adapter exists, this module cannot make
live external MCP calls.

Observability is metadata-only and best effort. The returned receipt determines
call semantics; an EventSink outage cannot make a completed call appear failed
and invite an unsafe retry.
