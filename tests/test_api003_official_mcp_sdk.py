"""API-003 interoperability check using the installed official MCP SDK."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp")
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "api003_provider.py"


def test_official_mcp_sdk_can_negotiate_list_and_call_sonder_fixture():
    async def exchange():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[str(FIXTURE)],
            cwd=str(REPO_ROOT),
            env={
                **os.environ,
                "PYTHONPATH": str(REPO_ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            },
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool("echo", {"value": "sdk"})
                return initialized, tools, result

    initialized, tools, result = asyncio.run(exchange())
    assert initialized.protocol_version == "2025-11-25"
    assert [tool.name for tool in tools.tools] == ["echo", "hang"]
    assert result.structured_content == {"output": {"echo": "sdk"}}
