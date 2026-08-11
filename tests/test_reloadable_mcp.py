import asyncio
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reloadable_mcp import ReloadableFastMCP, _recovery_action

# The sample modules below register their own `alpha`/`beta` tools on a private
# registry, so Sonder's command catalog -- which reads `server.mcp` -- has never
# heard of them and `risk_of` grades them `unclassified`. `_refuse_if_gated`
# then refuses them, correctly: these two tests drive `call_tool` through the
# real permission gate, and an unclassifiable name is precisely what it exists
# to stop. Before the fail-closed fix they passed because the unknown-tool
# fallback graded `ask`, which a non-interactive caller was allowed.
#
# Granted here with an explicit `allow` rule rather than by disabling the gate:
# that is the operator escape hatch `decide()` documents for exactly this
# situation, so these tests go on exercising a live gate instead of proving
# reload mechanics work with enforcement switched off.
_SAMPLE_TOOLS = {"alpha", "beta"}


@pytest.fixture
def allow_sample_tools(monkeypatch):
    import permission_modes

    monkeypatch.setattr(
        permission_modes,
        "_rule_lookup",
        lambda name: ({"action": permission_modes.ALLOW, "pattern": name}
                      if str(name or "").lstrip("/") in _SAMPLE_TOOLS else None),
    )


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
        assert state["provenance"]["configured_root_ready"] is False
        assert "Set SONDER_RUNTIME_ROOT" in (
            state["provenance"]["recovery_action"]
        )
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


def test_configured_root_requires_real_module_entrypoint(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    source_root = tmp_path / "loaded"
    configured_root = tmp_path / "candidate"
    source_root.mkdir()
    configured_root.mkdir()
    module_name = "reloadable_mcp_entrypoint_shape_sample"
    module_path = source_root / (module_name + ".py")
    module_path.write_text(_module_source("stable"), encoding="utf-8")
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", str(configured_root))
    monkeypatch.syspath_prepend(str(source_root))
    try:
        mcp = importlib.import_module(module_name).mcp
        for relative in (
            "server.py",
            "sonder_runtime/__init__.py",
            "sonder_runtime/__main__.py",
        ):
            target = configured_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# entrypoint\n", encoding="utf-8")
            state = mcp.runtime_snapshot()["provenance"]
            expected_ready = relative == "sonder_runtime/__main__.py"
            assert state["configured_root_ready"] is expected_ready
            if not expected_ready:
                assert "Set SONDER_RUNTIME_ROOT" in state["recovery_action"]
        assert "working directory SONDER_RUNTIME_ROOT" in (
            mcp.runtime_snapshot()["provenance"]["recovery_action"]
        )
    finally:
        sys.modules.pop(module_name, None)


def test_expanduser_runtime_error_fails_closed_with_last_known_good(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    source_root = tmp_path / "loaded"
    source_root.mkdir()
    module_name = "reloadable_mcp_expanduser_failure_sample"
    module_path = source_root / (module_name + ".py")
    module_path.write_text(_module_source("stable"), encoding="utf-8")
    configured_text = "~/private-runtime-root"
    monkeypatch.setenv("SONDER_RUNTIME_ROOT", configured_text)
    monkeypatch.syspath_prepend(str(source_root))
    original_expanduser = Path.expanduser
    try:
        mcp = importlib.import_module(module_name).mcp
        loaded_digest = mcp.runtime_snapshot()["loaded_digest"]
        _write_new_source(module_path, _module_source("changed"))

        def fail_configured_expanduser(path):
            if str(path) == configured_text:
                raise RuntimeError("secret home lookup failure")
            return original_expanduser(path)

        monkeypatch.setattr(Path, "expanduser", fail_configured_expanduser)
        refreshed = mcp.refresh_if_changed()
        state = mcp.runtime_snapshot()

        assert refreshed == {
            "reloaded": False,
            "surface_changed": False,
            "error": "configured runtime root is unavailable",
        }
        assert mcp._tool_manager.get_tool("alpha").fn() == "stable"
        assert state["loaded_digest"] == loaded_digest
        assert state["provenance"]["issue"] == "configured_root_missing"
        assert state["provenance"]["configured_runtime_root"] == "(set)"
        assert configured_text not in repr(state)
        assert "secret home lookup failure" not in repr(state)
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


def test_tool_call_refresh_sends_list_changed_notification(
    monkeypatch, tmp_path, allow_sample_tools,
):
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


def test_a_registry_swap_drops_the_memoised_command_catalog(monkeypatch, tmp_path,
                                                            allow_sample_tools):
    """The catalog is an `lru_cache` over the registry this swap replaces.

    `command_catalog.reset_cache()` existed, its docstring said "used after a
    live reload adds tools", and it had **no callers anywhere** -- so the
    catalog was memoised across every hot swap for the life of the process.
    That is not only a staleness bug: the catalog is the permission gate's one
    source of truth for a tool's risk class, so a reload that reclassified a
    tool `safe` -> `dangerous` left the gate enforcing the old grade, and a
    newly added tool was ungraded entirely.

    Asserted by observing the invalidation rather than the cache's end state:
    the permission gate runs immediately after the swap, on the same
    `call_tool`, and re-warms the catalog before this test could look at it --
    so `currsize == 0` afterwards is never true and would make this a test
    that can only fail. `misses` is checked too, which is the evidence the
    entry really was recomputed rather than served stale.
    """
    import command_catalog

    monkeypatch.setenv("SONDER_LIVE_RELOAD", "1")
    module_name = "reloadable_mcp_cachebust_sample"
    module_path = tmp_path / (module_name + ".py")
    module_path.write_text(_module_source("before"), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        module = importlib.import_module(module_name)
        mcp = module.mcp
        context = SimpleNamespace(
            request_context=SimpleNamespace(
                session=SimpleNamespace(
                    send_tool_list_changed=_noop_async,
                ),
            ),
        )
        monkeypatch.setattr(mcp, "get_context", lambda: context)

        # Warm the cache, and prove it is warm -- otherwise an invalidation
        # would be indistinguishable from nothing ever having been cached.
        command_catalog.catalog()
        command_catalog.catalog()
        assert command_catalog.catalog.cache_info().currsize == 1
        assert command_catalog.catalog.cache_info().hits >= 1, (
            "the second call should have been a cache hit; if it was not, "
            "'the cache was cleared' below proves nothing"
        )

        real_reset = command_catalog.reset_cache
        resets = []

        def spy():
            resets.append(1)
            real_reset()

        monkeypatch.setattr(command_catalog, "reset_cache", spy)

        _write_new_source(module_path, _module_source("after", include_beta=True))
        asyncio.run(mcp.call_tool("alpha", {}))

        assert resets, (
            "the registry was swapped and the memoised catalog was never "
            "invalidated, so the permission gate keeps grading tools from the "
            "pre-reload registry"
        )
        # `cache_clear()` zeroes the hit/miss counters as well as the entry, so
        # the pre-swap hit recorded above must be gone. Anything still served
        # from the old memoised entry would have carried it forward.
        assert command_catalog.catalog.cache_info().hits == 0, (
            "reset_cache ran but the catalog was still served from the "
            "pre-reload memoised entry"
        )
    finally:
        sys.modules.pop(module_name, None)
        command_catalog.reset_cache()


async def _noop_async():
    return None


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

    # This one really does spawn a child process, so the `allow_sample_tools`
    # monkeypatch cannot reach it -- the grant has to be on disk where the
    # child's own `_default_rule_lookup` will find it. A tmp SONDER_HOME also
    # keeps the child off the operator's real home. See `_SAMPLE_TOOLS` above
    # for why a grant is needed at all.
    sonder_home = tmp_path / "sonder_home"
    sonder_home.mkdir()
    (sonder_home / "permissions.json").write_text(
        json.dumps([{"pattern": name, "action": "allow"} for name in sorted(_SAMPLE_TOOLS)],
                   indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SONDER_HOME", str(sonder_home))

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
