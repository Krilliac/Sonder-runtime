"""Small separately launched MCP provider used by API-003 boundary tests."""
from __future__ import annotations

import sys

from sonder_runtime.application.protocol.mcp_compatibility import McpCompatibility, SubscriptionNotificationRouter
from sonder_runtime.interfaces.mcp.transport import McpTransportLimits, StdioMcpTransport


def _main() -> None:
    notifications = "--notifications" in sys.argv[1:]
    router = SubscriptionNotificationRouter() if notifications else None

    def handle(name, arguments):
        if name == "hang":
            import time
            time.sleep(30)
        if name != "echo":
            raise KeyError(name)
        if router is not None:
            router.publish("progress", {"value": arguments.get("value")})
        return {"output": {"echo": arguments.get("value")}}

    catalog = [{"name": "echo", "description": "external fixture echo", "inputSchema": {"type": "object"}}]
    if not notifications:
        catalog.append({"name": "hang", "description": "termination fixture", "inputSchema": {"type": "object"}})
    transport = StdioMcpTransport(
        sys.stdin, sys.stdout,
        compatibility=McpCompatibility(server_version="2.0", supported_versions=("2.0",), capabilities=("tools",)),
        tool_catalog=tuple(catalog), tool_handler=handle, notifications=router,
        connection_id="external-fixture",
        limits=McpTransportLimits(max_frame_bytes=4096, max_arguments_bytes=64),
    )
    transport.serve()


if __name__ == "__main__":
    _main()
