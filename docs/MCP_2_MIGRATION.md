# MCP 1.x → 2.x migration

Sonder's runtime dependency moved from `mcp==1.29.0` to `mcp==2.0.0`. MCP 2.x
retired the `FastMCP` name and the `mcp.server.fastmcp` package; the ergonomic
server it provided is now `MCPServer` in `mcp.server.mcpserver`. Nothing in
Sonder's tool surface changed — `@mcp.tool()`, `@mcp.resource(...)` and
`@mcp.prompt(...)` are registered exactly as before.

This file is the per-symbol map, written down because the failure mode is a
clean-looking `ImportError` at process start with no hint of the new name:

```
ImportError: cannot import name 'FastMCP' from 'mcp.server.fastmcp' (unknown location)
```

## Symbol map

| MCP 1.x | MCP 2.x |
|---|---|
| `mcp.server.fastmcp.FastMCP` | `mcp.server.mcpserver.MCPServer` |
| `mcp.server.fastmcp.exceptions.ToolError` | `mcp.server.mcpserver.exceptions.ToolError` |
| `mcp.server.fastmcp.tools.ToolManager` | `mcp.server.mcpserver.tools.ToolManager` |
| `mcp.server.fastmcp.resources.ResourceManager` | `mcp.server.mcpserver.resources.ResourceManager` |
| `mcp.server.fastmcp.prompts.PromptManager` | `mcp.server.mcpserver.prompts.PromptManager` |
| `FastMCP._mcp_server` | `MCPServer._lowlevel_server` |
| `FastMCP.get_context()` | *removed* — see "The context change" |
| `capabilities.tools.listChanged` | `capabilities.tools.list_changed` |
| `call_tool()` → `(blocks, structured)` | `call_tool()` → `CallToolResult` |
| `message_handler` receives `ServerNotification` (`.root`) | receives the concrete notification model |

`mcp.server.lowlevel.server.NotificationOptions` kept its name and its three
fields; `Server.create_initialization_options` gained a third parameter,
`extensions`.

## The context change

MCP 1.x exposed the in-flight request ambiently through `FastMCP.get_context()`,
so any method could reach the client session. 2.x removed that accessor: the
request context is passed as an argument to the `_handle_*` protocol entry
points, and `list_tools()` / `list_resources()` / `list_prompts()` now take no
context at all.

`ReloadableMCPServer` splits along that seam:

- **the public surface** (`list_tools`, `call_tool`, `read_resource`, …)
  refreshes the registry, because every caller — in-process or protocol —
  reaches it there, and applies the permission gate in `call_tool`;
- **the `_handle_*` overrides** additionally send
  `notifications/{tools,resources,prompts}/list_changed`, because they receive
  a `ServerRequestContext` and are therefore the only place a session exists.

An in-process caller has no session to notify in the first place, so nothing is
lost by the split.

## The two notification eras (the trap)

`list_changed` events travel on a **different channel per protocol era**, and one
server holds connections from both:

| Client era | Channel | Call |
|---|---|---|
| `<= 2025-11-25` | connection channel | `session.send_tool_list_changed()` |
| `2026-07-28` | `subscriptions/listen` stream | `bus.publish(ToolsListChanged())` |

At 2026-07-28 the connection channel **silently discards** change notifications:
`NotifyOnlyOutbound.notify` drops every method in `LISTEN_STREAM_METHODS` with a
debug log, because the era forbids sending a change notification a subscription
did not request. Its own docstring says "Publish them on the server's
`SubscriptionBus` instead."

This is the trap the migration walked into. MCP 1.x had only the connection
channel, so a direct port keeps working for legacy clients and strands every
modern one: the registry swaps, the new surface is live, and the client is never
told — it keeps calling tools the reload removed and never sees the ones it
added. Nothing raises. Nothing logs above debug. `_last_notification_error`
stays empty.

`ReloadableMCPServer._notify_surface_changes` therefore sends on **both**
channels unconditionally; whichever era did not want the copy discards it. That
costs less than an era assumption that rots at the next protocol version.

A default `Client(mode="auto")` negotiates the modern era, so this is the common
case, not the exotic one. `tests/test_reloadable_mcp.py` covers both:
`test_a_modern_client_is_told_the_surface_changed` opens a real
`subscriptions/listen` stream, and the stdio session test covers the legacy
handshake. **A test that connects with `ClientSession(...).initialize()` is on
the legacy era** — it cannot see a modern-era delivery failure, which is exactly
why the whole suite stayed green while every modern client went unnotified.

## Things that no longer need doing

**`_lowlevel_server._tool_cache` is gone.** MCP 1.x cached output schemas on the
low-level server, so a registry swap had to clear that cache or a rewritten tool
kept a stale schema. 2.x reads the schema off `Tool.fn_metadata.output_schema` on
the tool object the manager holds, so replacing the manager replaces the schema
with it. `tests/test_reloadable_mcp.py` asserts the attribute is *absent* — if a
later MCP reintroduces a server-level tool cache, that assertion fails and the
explicit clear has to come back, rather than the stale schema quietly returning
with nothing left watching for it.

**The `pydantic-settings` pin is gone.** MCP 2.x's server `Settings` is a plain
`BaseModel`, so the generic-lifespan forward reference that made
pydantic-settings 2.15 emit `IncompleteFieldDefinitionWarning` under MCP 1.x no
longer exists. Nothing in Sonder imports `pydantic_settings` directly; do not
re-add it as a runtime dependency without an importer.

**The `resource()` override no longer re-implements the decorator.** 2.x parses
the URI as a full RFC 6570 template and rejects shapes the old ad-hoc brace
regex accepted (and vice versa). `ReloadableMCPServer.resource()` delegates
registration upstream and redirects only the manager the decorator writes into.

That redirect is a **thread-local**, not an instance attribute. Upstream's
decorator writes to whatever `self._resource_manager` names when it runs, so the
redirect has to exist — but publishing it on the instance would make the
half-built staging registry the live one for the duration of every decorator
call, which is precisely what the staged swap exists to prevent. Scoped to the
reloading stack, every other reader keeps seeing the last known good registry.
`test_the_staging_redirect_is_scoped_to_the_reloading_thread` guards it.

## Sealed engine bundles

A bundle seals a Python runtime with the dependency set it was built against.
Nothing recorded *which* set, so a source-only update could carry the checkout
past the runtime pin while the bundle still held the old one: the bundle
validated cleanly, then `server.py` died at import. There was no way back —
`bootstrap_engine.py` forced offline whenever a bundle was present and
`ensure_python_deps` refuses pip offline, so the repair exited 3 and only
rebuilding the payload recovered.

Both halves are closed:

- `ENGINE-BUNDLE.json` is **schema 2** and carries a required `runtime_contract`
  — a sha256 over the pins in `requirements-runtime.txt`, normalised so a CRLF
  and an LF checkout of the same pins agree. Schema-1 bundles are rejected
  rather than upgraded: they were sealed before anything recorded their
  dependency set, so a current one cannot be told from a stale one.
- A present bundle no longer forbids dependency repair. Models still come from
  the bundle offline; only an explicit `--offline` forbids pip. A healthy bundle
  never reaches pip anyway — the compatibility probe short-circuits first — so
  this costs nothing until the sealed runtime is genuinely stale, which is the
  one moment pip is the only thing that can help.

## Where the version is enforced

`requirements-runtime.txt` is the single contract; `requirements-dev.txt`
includes it. The compatibility probe — `from mcp.server.mcpserver import
MCPServer; from mcp.server.mcpserver.tools import ToolManager` — runs in four
places, and all four must agree:

- `.github/workflows/ci.yml` ("Verify MCP compatibility API")
- `deploy_sonder.sh`, after the venv install
- `bootstrap_engine.py` (`MCPSERVER_IMPORT_PROBE`), which fails closed when the
  API is unavailable after installing the runtime contract
- `scripts/assemble_engine_bundle.py` (`_MCPSERVER_IMPORT_PROBE`), which refuses
  to assemble a bundle around a Python runtime that cannot import it

`tests/test_mcp_dependency.py` asserts the pin is exact and that the retired
`mcp.server.fastmcp` module name survives in neither installer — a probe that
still imports it would only pass on the version being retired.

**The sealed engine bundle needs `cryptography`.** mcp 2.x imports it eagerly
(`mcp/server/request_state.py`), which 1.x never did, and it is reachable only
through `Requires-Dist: pyjwt[crypto]` — an extra-gated edge. The assembler's
dependency walk now carries extras through, and additionally seeds the closure
from `requirements-runtime.txt` so anything Sonder imports directly does not
depend on MCP happening to pull it in. Without both, every Windows bundle build
fails its own import probe.
`tests/test_assemble_engine_bundle.py::test_the_sealed_closure_satisfies_the_import_probe`
copies the closure and runs the real probe against it — the other tests in that
file supply a runtime or monkeypatch `subprocess.run`, so "the bundle tests
pass" has never on its own meant "a bundle can be built".
