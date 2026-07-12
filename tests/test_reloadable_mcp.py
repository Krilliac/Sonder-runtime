import asyncio
import importlib
import os
import sys
import time
from types import SimpleNamespace

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reloadable_mcp import ReloadableFastMCP


def _module_source(version, *, include_beta=False):
    beta = (
        """
@mcp.tool()
def beta() -> str:
    return "beta"
"""
        if include_beta
        else ""
    )
    return f'''from reloadable_mcp import ReloadableFastMCP

existing = globals().get("_PERSISTENT_MCP")
if isinstance(existing, ReloadableFastMCP):
    mcp = existing
    mcp.begin_module_refresh()
else:
    mcp = ReloadableFastMCP("sample")
_PERSISTENT_MCP = mcp

@mcp.tool()
def alpha() -> str:
    return "{version}"
{beta}
mcp.finish_module_refresh(__name__, __file__, globals())
'''


def _write_new_source(path, text):
    path.write_text(text, encoding="utf-8")
    future = time.time() + 2
    os.utime(path, (future, future))


def _stdio_source(version, *, include_beta=False):
    return (
        _module_source(version, include_beta=include_beta)
        + """
if __name__ == "__main__" and not globals().get("_MCP_HOT_RELOAD_EXEC"):
    mcp.run()
"""
    )


def test_registry_refresh_adds_updates_and_removes_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_module_source("v1"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        mcp = module.mcp
        assert isinstance(mcp, ReloadableFastMCP)
        assert mcp._tool_manager.get_tool("alpha").fn() == "v1"
        assert (
            mcp._mcp_server.create_initialization_options().capabilities.tools.listChanged
            is True
        )

        mcp._mcp_server._tool_cache["alpha"] = object()
        _write_new_source(module_path, _module_source("v1-implementation-update"))
        refreshed = mcp.refresh_if_changed()

        assert refreshed == {"reloaded": True, "surface_changed": False}
        assert mcp._tool_manager.get_tool("alpha").fn() == "v1-implementation-update"
        assert mcp._mcp_server._tool_cache == {}

        _write_new_source(module_path, _module_source("v2", include_beta=True))
        refreshed = mcp.refresh_if_changed()

        assert refreshed == {"reloaded": True, "surface_changed": True}
        assert mcp._tool_manager.get_tool("alpha").fn() == "v2"
        assert mcp._tool_manager.get_tool("beta").fn() == "beta"
        assert mcp.runtime_snapshot()["refresh_count"] == 2

        _write_new_source(module_path, _module_source("v3"))
        refreshed = mcp.refresh_if_changed()

        assert refreshed == {"reloaded": True, "surface_changed": True}
        assert mcp._tool_manager.get_tool("alpha").fn() == "v3"
        assert mcp._tool_manager.get_tool("beta") is None
        assert mcp.runtime_snapshot()["refresh_count"] == 3
    finally:
        sys.modules.pop(module_name, None)


def test_broken_refresh_preserves_last_known_good_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_failure_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(
        _module_source("stable", include_beta=True), encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        mcp = module.mcp
        loaded = mcp.runtime_snapshot()["loaded_digest"]

        _write_new_source(module_path, "def broken(:\n")
        refreshed = mcp.refresh_if_changed()
        state = mcp.runtime_snapshot()

        assert refreshed["reloaded"] is False
        assert refreshed["error"].startswith("SyntaxError")
        assert mcp._tool_manager.get_tool("alpha").fn() == "stable"
        assert mcp._tool_manager.get_tool("beta").fn() == "beta"
        assert state["loaded_digest"] == loaded
        assert state["source_changed"] is True
        assert state["status"] == "error"
    finally:
        sys.modules.pop(module_name, None)


def test_refresh_tracks_exact_executed_bytes_when_source_changes_during_exec(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_racing_editor_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_module_source("v1"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        mcp = module.mcp
        next_source = _module_source("v3")
        changing_source = _module_source("v2").replace(
            "mcp.finish_module_refresh(__name__, __file__, globals())",
            "from pathlib import Path\n"
            f"Path(__file__).write_text({next_source!r}, encoding='utf-8')\n"
            "mcp.finish_module_refresh(__name__, __file__, globals())",
        )

        _write_new_source(module_path, changing_source)
        refreshed = mcp.refresh_if_changed()
        state = mcp.runtime_snapshot()

        assert refreshed == {"reloaded": True, "surface_changed": False}
        assert mcp._tool_manager.get_tool("alpha").fn() == "v2"
        assert state["loaded_digest"] != state["current_digest"]
        assert state["source_changed"] is True

        refreshed = mcp.refresh_if_changed()
        assert refreshed == {"reloaded": True, "surface_changed": False}
        assert mcp._tool_manager.get_tool("alpha").fn() == "v3"
    finally:
        sys.modules.pop(module_name, None)


def test_tool_call_refresh_sends_list_changed_notification(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_notification_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_module_source("before"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        mcp = module.mcp
        notifications = []

        class Session:
            async def send_tool_list_changed(self):
                notifications.append("changed")

        context = SimpleNamespace(
            request_context=SimpleNamespace(session=Session()),
        )
        monkeypatch.setattr(mcp, "get_context", lambda: context)
        _write_new_source(module_path, _module_source("after", include_beta=True))

        result = asyncio.run(mcp.call_tool("alpha", {}))

        assert notifications == ["changed"]
        assert result[0][0].text == "after"
        assert mcp._tool_manager.get_tool("beta").fn() == "beta"
    finally:
        sys.modules.pop(module_name, None)


def test_server_uses_reloadable_registry_and_reports_current_source(monkeypatch):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    import server

    state = server.mcp_runtime_data()

    assert isinstance(server.mcp, ReloadableFastMCP)
    assert state["status"] == "current"
    assert state["registered_tools"] >= 100
    assert state["protocol_list_changed"] is True
    assert state["loaded_digest"] == state["current_digest"]
    assert "last known-good registry" not in server.format_mcp_runtime(state)
    assert "status: current" in server.control_command("/mcp status")
    assert "/mcp" in server.command_registry_list("mcp")


def test_real_stdio_session_hot_adds_updates_removes_and_fails_closed(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    server_path = tmp_path / "stdio_reload_server.py"
    server_path.write_text(_stdio_source("v1"), encoding="utf-8")
    repo_root = os.path.dirname(os.path.dirname(__file__))
    notifications = []

    async def exercise():
        async def handle_message(message):
            root = getattr(message, "root", None)
            if type(root).__name__ == "ToolListChangedNotification":
                notifications.append("tools/list_changed")

        params = StdioServerParameters(
            command=sys.executable,
            args=[str(server_path)],
            cwd=str(tmp_path),
            env={**os.environ, "PYTHONPATH": repo_root},
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(
                read_stream,
                write_stream,
                message_handler=handle_message,
            ) as session:
                initialized = await session.initialize()
                assert initialized.capabilities.tools.listChanged is True
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["alpha"]

                _write_new_source(
                    server_path,
                    _stdio_source("v2", include_beta=True),
                )
                result = await session.call_tool("alpha", {})
                assert result.content[0].text == "v2"
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["alpha", "beta"]

                _write_new_source(server_path, "def broken(:\n")
                result = await session.call_tool("alpha", {})
                assert result.content[0].text == "v2"

                _write_new_source(server_path, _stdio_source("v3"))
                result = await session.call_tool("alpha", {})
                assert result.content[0].text == "v3"
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["alpha"]

    asyncio.run(exercise())

    assert notifications == ["tools/list_changed", "tools/list_changed"]
