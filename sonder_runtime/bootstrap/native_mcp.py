"""Application-owned MCP composition for the native transport.

This is intentionally a bounded migration surface.  Its catalog is derived
from the tools currently owned by ``ToolExecutorAdapter``; the historical
server catalog remains a separate, explicit compatibility mode until parity
is demonstrated.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import TextIO

from ..application.context import local_owner_context
from ..application.ports.tool_executor import ToolCall
from ..application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from ..application.protocol.mcp_compatibility import McpCompatibility
from ..interfaces.mcp.transport import StdioMcpTransport


_NATIVE_TOOLS = (
    ToolDescriptor("edit_file", "Apply a bounded text edit", {"type": "object"}),
    ToolDescriptor("make_directory", "Create a directory under an allowed root", {"type": "object"}),
    ToolDescriptor("read_file", "Read a bounded file", {"type": "object"}),
    ToolDescriptor("run_program", "Run an argv-based program", {"type": "object"}),
    ToolDescriptor("run_script", "Run a bounded script", {"type": "object"}),
    ToolDescriptor("write_file", "Write a file under an allowed root", {"type": "object"}),
)


def native_tool_registry() -> InMemoryToolRegistry:
    """Return the immutable-at-composition catalog for native MCP tools."""
    return InMemoryToolRegistry(_NATIVE_TOOLS)


def run_native_mcp(application, *, input_stream: TextIO | None = None,
                   output_stream: TextIO | None = None) -> int:
    """Serve native MCP over stdio using the application tool port."""
    config = application.config
    roots = tuple(
        Path(root)
        for root in (config.state.workspace_roots if config is not None else ())
    )
    registry = native_tool_registry()

    def execute(name: str, arguments: dict) -> dict:
        context = local_owner_context(
            correlation_id=uuid.uuid4().hex,
            source="mcp",
            workspace_roots=roots,
            timeout_seconds=60.0,
        )
        result = application.tool_executor.execute(
            ToolCall(tool=name, arguments=dict(arguments)), context
        )
        return {
            "output": result.output,
            "isError": not result.ok,
            "error": result.error_code,
            "evidence": dict(result.evidence or {}),
        }

    transport = StdioMcpTransport(
        input_stream or sys.stdin,
        output_stream or sys.stdout,
        compatibility=McpCompatibility(
            server_version="2.0", supported_versions=("2.0",),
            capabilities=("tools", "notifications"),
        ),
        tool_catalog=registry,
        tool_handler=execute,
    )
    return transport.serve()


__all__ = ["native_tool_registry", "run_native_mcp"]
