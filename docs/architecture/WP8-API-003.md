# WP8 API-003 — MCP compatibility foundation

This slice defines the application-owned MCP compatibility boundary. It keeps
MCP 2.x as the preferred protocol, permits only explicitly declared legacy
contracts, and routes negotiated notifications through local subscriptions.

`McpCompatibility` performs deterministic version and capability negotiation;
it does not import an MCP SDK or perform provider/network I/O. The
`SubscriptionNotificationRouter` is likewise an in-process delivery contract.
An interface adapter remains responsible for encoding these results on the
actual MCP transport and for any externally authorized connection lifecycle.

A negotiation carries two capability sets. `server_capabilities` is everything
the server supports, and it is what the `initialize` result advertises: MCP has
each side declare its own capabilities, so a client that sends
`capabilities: {}` still learns that `tools` is served. `capabilities` is the
intersection with the client's advertised keys, and features that need the
client's opt-in (MCP Tasks) stay gated on it; the native transport advertises
such a capability only when it was negotiated, so `initialize` never promises
a method the session would refuse. The native transport reports the
runtime build version (`sonder_runtime.platform.version.runtime_version()`) as
`serverInfo.version`.

Focused coverage verifies preferred-version selection, fail-closed legacy
negotiation, capability intersection, subscription delivery, and removal.
Formal API-003 checklist status remains unchanged until the complete MCP v2
interface migration and end-to-end compatibility audit are complete.
