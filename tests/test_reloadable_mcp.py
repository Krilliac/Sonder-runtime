import asyncio
import importlib
import os
import shutil
import sys
import time
from types import SimpleNamespace

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reloadable_mcp import ReloadableFastMCP, _recovery_action


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


def _primitive_source(version, *, include_extra=False, fail=False):
    extra = (
        '''
@mcp.resource("sonder://extra")
def extra_resource() -> str:
    return "extra"

@mcp.prompt("extra_prompt")
def extra_prompt() -> str:
    return "extra"
'''
        if include_extra
        else ""
    )
    failure = 'raise RuntimeError("refresh failed")' if fail else ""
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

@mcp.resource("sonder://state")
def state_resource() -> str:
    return "{version}"

@mcp.resource("sonder://item/{{name}}")
def item_resource(name: str) -> str:
    return name

@mcp.prompt("state_prompt")
def state_prompt() -> str:
    return "{version}"
{extra}
{failure}
mcp.finish_module_refresh(__name__, __file__, globals())
'''


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


def test_deleted_windows_source_root_reports_restart_provenance(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    runtime_root = tmp_path / "Deleted Sonder Worktree"
    runtime_root.mkdir()
    module_name = "reloadable_mcp_deleted_root_sample"
    module_path = runtime_root / (module_name + ".py")
    module_path.write_text(_module_source("stable"), encoding="utf-8")
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.syspath_prepend(str(runtime_root))
    try:
        mcp = importlib.import_module(module_name).mcp
        shutil.rmtree(runtime_root)

        refreshed = mcp.refresh_if_changed()
        state = mcp.runtime_snapshot()

        assert refreshed["reloaded"] is False
        assert refreshed["error"] == (
            "stale runtime source: loaded MCP file is unavailable"
        )
        assert state["status"] == "error"
        assert state["provenance"]["issue"] == "stale_source_root"
        assert state["provenance"]["source_root"] == "(loaded source root)"
        assert state["provenance"]["source_root_exists"] is False
        assert state["provenance"]["configured_root_exists"] is False
        assert "Set SONDER_RUNTIME_ROOT" in (
            state["provenance"]["recovery_action"]
        )
        assert "python -m sonder_runtime mcp" in (
            state["provenance"]["recovery_action"]
        )
        assert str(tmp_path) not in repr(state)
    finally:
        sys.modules.pop(module_name, None)


def test_runtime_provenance_detects_configured_root_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    source_root = tmp_path / "old-worktree"
    canonical_root = tmp_path / "canonical-runtime"
    source_root.mkdir()
    canonical_root.mkdir()
    (canonical_root / "server.py").write_text("# canonical\n", encoding="utf-8")
    module_name = "reloadable_mcp_root_mismatch_sample"
    module_path = source_root / (module_name + ".py")
    module_path.write_text(_module_source("stable"), encoding="utf-8")
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(canonical_root))
    monkeypatch.syspath_prepend(str(source_root))
    try:
        mcp = importlib.import_module(module_name).mcp
        _write_new_source(module_path, _module_source("changed"))
        refreshed = mcp.refresh_if_changed()
        state = mcp.runtime_snapshot()

        assert refreshed == {
            "reloaded": False,
            "surface_changed": False,
            "error": "loaded MCP source does not match configured runtime root",
        }
        assert mcp._tool_manager.get_tool("alpha").fn() == "stable"
        assert state["provenance"]["issue"] == "root_mismatch"
        assert state["provenance"]["root_matches_configured"] is False
        assert str(canonical_root) not in repr(state)
        assert "python -m sonder_runtime mcp" in (
            state["provenance"]["recovery_action"]
        )

        monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(source_root))
        assert mcp.refresh_if_changed()["reloaded"] is True
        assert mcp._tool_manager.get_tool("alpha").fn() == "changed"
        assert mcp.runtime_snapshot()["status"] == "current"
    finally:
        sys.modules.pop(module_name, None)


def test_missing_configured_root_fails_closed_with_last_known_good(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    source_root = tmp_path / "loaded"
    source_root.mkdir()
    module_name = "reloadable_mcp_missing_configured_sample"
    module_path = source_root / (module_name + ".py")
    module_path.write_text(_module_source("stable"), encoding="utf-8")
    missing = tmp_path / "missing-secret-root"
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(missing))
    monkeypatch.syspath_prepend(str(source_root))
    try:
        mcp = importlib.import_module(module_name).mcp
        _write_new_source(module_path, _module_source("changed"))

        refreshed = mcp.refresh_if_changed()
        state = mcp.runtime_snapshot()

        assert refreshed["error"] == "configured runtime root is unavailable"
        assert mcp._tool_manager.get_tool("alpha").fn() == "stable"
        assert state["status"] == "error"
        assert state["provenance"]["issue"] == "configured_root_missing"
        assert str(missing) not in repr(state)
    finally:
        sys.modules.pop(module_name, None)


def test_recreated_source_is_rehashed_after_stale_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    module_name = "reloadable_mcp_recreated_sample"
    module_path = runtime_root / (module_name + ".py")
    module_path.write_text(_module_source("stable"), encoding="utf-8")
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.syspath_prepend(str(runtime_root))
    try:
        mcp = importlib.import_module(module_name).mcp
        original = module_path.stat()
        shutil.rmtree(runtime_root)
        assert mcp.refresh_if_changed()["reloaded"] is False

        runtime_root.mkdir()
        module_path.write_text(_module_source("mutant"), encoding="utf-8")
        os.utime(
            module_path,
            ns=(original.st_atime_ns, original.st_mtime_ns),
        )
        assert module_path.stat().st_size == original.st_size

        refreshed = mcp.refresh_if_changed()

        assert refreshed["reloaded"] is True
        assert mcp._tool_manager.get_tool("alpha").fn() == "mutant"
        assert mcp.runtime_snapshot()["status"] == "current"
    finally:
        sys.modules.pop(module_name, None)


def test_normal_configured_root_remains_current(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_current_root_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_module_source("stable"), encoding="utf-8")
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mcp = importlib.import_module(module_name).mcp

        assert mcp.refresh_if_changed() == {
            "reloaded": False,
            "surface_changed": False,
        }
        assert mcp.runtime_snapshot()["status"] == "current"
        assert mcp.runtime_snapshot()["provenance"]["issue"] == ""
    finally:
        sys.modules.pop(module_name, None)


def test_recovery_actions_are_ascii_and_platform_portable():
    configured = _recovery_action(True)
    fallback = _recovery_action(False)

    assert "python -m sonder_runtime mcp" in configured
    assert "python -m sonder_runtime mcp" in fallback
    assert "working directory SONDER_RUNTIME_ROOT" in configured
    assert all(ord(char) < 128 for char in configured + fallback)


def test_refresh_atomically_replaces_resources_prompts_and_templates(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_primitives_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_primitive_source("v1", include_extra=True), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mcp = importlib.import_module(module_name).mcp
        assert len(mcp._resource_manager.list_resources()) == 2
        assert len(mcp._resource_manager.list_templates()) == 1
        assert len(mcp._prompt_manager.list_prompts()) == 2

        _write_new_source(module_path, _primitive_source("v2"))
        refreshed = mcp.refresh_if_changed()

        assert refreshed == {"reloaded": True, "surface_changed": True}
        assert [str(r.uri) for r in mcp._resource_manager.list_resources()] == [
            "sonder://state",
        ]
        assert len(mcp._resource_manager.list_templates()) == 1
        assert [p.name for p in mcp._prompt_manager.list_prompts()] == [
            "state_prompt"
        ]
        assert mcp.runtime_snapshot()["last_surface_changes"] == {
            "tools": False,
            "resources": True,
            "prompts": True,
        }
    finally:
        sys.modules.pop(module_name, None)


def test_failed_refresh_does_not_publish_resources_or_prompts(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_primitive_failure_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_primitive_source("stable"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mcp = importlib.import_module(module_name).mcp
        _write_new_source(
            module_path,
            _primitive_source("partial", include_extra=True, fail=True),
        )

        refreshed = mcp.refresh_if_changed()

        assert refreshed["reloaded"] is False
        assert [str(r.uri) for r in mcp._resource_manager.list_resources()] == [
            "sonder://state",
        ]
        assert [p.name for p in mcp._prompt_manager.list_prompts()] == [
            "state_prompt"
        ]
    finally:
        sys.modules.pop(module_name, None)


def test_primitive_refresh_advertises_and_sends_list_changed(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_primitive_notification_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_primitive_source("before"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mcp = importlib.import_module(module_name).mcp
        capabilities = mcp._mcp_server.create_initialization_options().capabilities
        assert capabilities.resources.listChanged is True
        assert capabilities.prompts.listChanged is True
        notifications = []

        class Session:
            async def send_tool_list_changed(self):
                notifications.append("tools")

            async def send_resource_list_changed(self):
                notifications.append("resources")

            async def send_prompt_list_changed(self):
                notifications.append("prompts")

        context = SimpleNamespace(
            request_context=SimpleNamespace(session=Session()),
        )
        monkeypatch.setattr(mcp, "get_context", lambda: context)
        _write_new_source(module_path, _primitive_source("after", include_extra=True))

        asyncio.run(mcp.list_resources())

        assert notifications == ["resources", "prompts"]
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


def test_disabled_reload_with_pending_edit_is_not_reported_as_refresh_pending(
    monkeypatch, tmp_path,
):
    """When live reload is OFF, an on-disk edit will never be applied, so the
    status must say disabled -- not "refresh pending", which tells an operator
    a refresh is imminent when the registry will stay frozen forever."""
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_disabled_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_module_source("v1"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        mcp = module.mcp
        assert mcp.runtime_snapshot()["status"] == "current"

        # Now disable reload, then edit the source: source_changed becomes True
        # but no refresh will ever run.
        monkeypatch.setenv("SONDER_LIVE_RELOAD", "0")
        _write_new_source(module_path, _module_source("v2", include_beta=True))
        # refresh_if_changed short-circuits while disabled -> registry frozen.
        assert mcp.refresh_if_changed().get("reloaded") is False

        status = mcp.runtime_snapshot()["status"]
        assert status.startswith("disabled"), (
            "a frozen registry with a pending edit must not read as refresh pending: %r"
            % status
        )
        assert "pending edit ignored" in status
    finally:
        sys.modules.pop(module_name, None)
