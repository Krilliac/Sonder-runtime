# Sonder Runtime developer SDK

`sonder_runtime.sdk` is a dependency-free Python seam for capability discovery,
typed tool calls, structured errors, and declarative plugin manifests. It is a
projection over the runtime's generated catalogs and tool gateway; it is not a
second executor or permission system.

**The SDK is in-process only (2026-11-20).** It runs inside a Python process
that already holds a composed runtime. It ships no HTTP, MCP, or other remote
transport: `GatewayTransport` is the only bundled transport that reaches a
runtime, and it accepts only a `ToolApplicationFacade` (see below).
`CallableTransport` only adapts callables you supply. A program in another process or on
another machine should call the runtime's HTTP API instead; see
[HTTP API & lifecycle](wiki/05-http-api-and-lifecycle.md) and, for a remote
host, [Connect clients](runbooks/connect-clients.md).

## What the facade is and where it comes from

`tool_facade` below is a
`sonder_runtime.application.tools.facade.ToolApplicationFacade`. The SDK does
not create one. A host composes it with `ToolApplicationFacade.compose(...)`;
the packaged runtime does this in `build_runtime()`
(`sonder_runtime/adapters/runtime_container.py`) and carries the result as the
`tools` field of the returned `Runtime` (the field is `None` when the runtime
was composed without it). `GatewayTransport` raises `TypeError` for any other
object, and also when `request_factory` is not callable. The public import
surface is `sonder_runtime.sdk`, which re-exports the implementation in
`sonder_runtime/interfaces/sdk/`.

## Minimal in-process use

```python
from sonder_runtime.platform.version import build_info
from sonder_runtime.sdk import GatewayTransport, SonderClient

# `tool_facade` is a host-composed ToolApplicationFacade. The host-owned
# request_factory resolves the authenticated principal, scope, permission
# effects, approval, deadline, and cancellation. Those values never come from
# the SDK request payload.
client = SonderClient(GatewayTransport(
    tool_facade,
    request_factory,
    runtime_version=build_info().version,
))

snapshot = client.refresh()
print(snapshot.runtime_version, [tool.name for tool in snapshot.tools])

result = client.call("status", {}, request_id="example-status-1")
if result.ok:
    print(result.output)
else:
    print(result.error.code, result.error.message, result.error.retryable)
```

To reach the runtime through another path, implement the two-method
`SdkTransport` protocol (`discover()` and `invoke(request)`) or wrap callables
with `CallableTransport`; no such HTTP or MCP transport is bundled. The
client validates discovered tool schemas before it sends a request and rejects
mismatched response IDs.

## Compatibility and safety contracts

- SDK wire protocol version: `1.0`. Discovery advertises all supported SDK and
  MCP versions, and negotiation fails closed when there is no common SDK
  version.
- Every request carries the generated catalog SHA-256 digest. A stale digest is
  rejected before gateway execution; callers should refresh discovery and make
  an explicit retry decision. The SDK never retries a possibly mutating call.
- Discovery's effects and execution class are descriptive. The
  `authorization` field is always `runtime-evaluated`; discovery is never an
  allow-list or permission receipt.
- SDK payloads contain no permission, approval, principal, workspace, or
  startup-authority fields. The host resolves them independently, and
  `GatewayTransport` rejects a request factory that rewrites call identity or
  arguments.
- Known runtime failures use stable `SonderError` codes and retryability.
  Unexpected exceptions become a generic `INTERNAL_FAILURE` without exposing
  exception text.

## Plugin manifests

`SdkPluginManifest.from_dict()` accepts only the documented v1 fields, checks
bounded identifiers and semantic versions, and rejects unknown keys. Runtime
compatibility uses an inclusive minimum and exclusive maximum. Permission,
dependency, and capability gaps are returned as typed diagnostics.

Calling `to_extension_manifest()` creates a declarative registry value only. It
does not load code, install an artifact, grant permissions, or bypass signature,
provenance, quarantine, health, and registry admission checks. The JSON Schema
projection is available as `PLUGIN_MANIFEST_JSON_SCHEMA` for editor/tooling
generation; the Python parser remains the authoritative validation path.

## Rollback

The SDK is additive. Existing MCP, HTTP, CLI, mobile, root compatibility, and
plugin-lite surfaces are unchanged. Removing `sonder_runtime/sdk.py`,
`sonder_runtime/interfaces/sdk/`, and this documentation restores the prior
public surface without a state or data migration.
