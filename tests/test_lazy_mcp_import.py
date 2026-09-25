"""``import server`` must not import the MCP SDK.

The SDK was ~40% of ``import server`` (about 0.9 s of 2.3 s under load), paid
by every REPL, HTTP, CLI and pytest-worker process that never serves MCP.
``reloadable_mcp.LazyReloadableMCPServer`` records the registry and builds it
on first use; these tests pin both halves of that contract in fresh
interpreters, where no earlier import can hide a regression.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(code: str, *args: str) -> dict:
    # Live reload on, so the built registry reports its source identity.
    env = dict(os.environ, SONDER_LIVE_RELOAD="1")
    completed = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-3000:]
    line = [row for row in completed.stdout.splitlines() if row.startswith("RESULT=")]
    assert line, completed.stdout[-2000:]
    return json.loads(line[-1].removeprefix("RESULT="))


def test_import_server_does_not_import_the_mcp_sdk():
    result = _run(
        "import json, sys\n"
        "import server\n"
        "print('RESULT=' + json.dumps({\n"
        "    'mcp': sorted(m for m in sys.modules if m == 'mcp' or m.startswith(('mcp.', 'mcp_types'))),\n"
        "    'registry': type(server.__dict__['mcp']).__name__,\n"
        "}))\n"
    )
    assert result["mcp"] == [], result
    assert result["registry"] == "LazyReloadableMCPServer"


def test_cli_status_help_does_not_import_server_or_mcp():
    result = _run(
        "import json, runpy, sys\n"
        "sys.argv = ['sonder_runtime', 'status', '--help']\n"
        "try:\n"
        "    runpy.run_module('sonder_runtime', run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "print('RESULT=' + json.dumps({\n"
        "    'mcp': 'mcp' in sys.modules, 'server': 'server' in sys.modules,\n"
        "}))\n"
    )
    assert result == {"mcp": False, "server": False}


def test_first_registry_use_builds_the_full_surface_from_the_lazy_record():
    """The deferred registry, once built, is the one the eager path produced."""
    result = _run(
        "import json, sys\n"
        "import server, reloadable_mcp\n"
        "assert 'mcp' not in sys.modules\n"
        "registry = server.mcp\n"
        "tools = sorted(t.name for t in registry._tool_manager.list_tools())\n"
        "state = server.mcp_runtime_data()\n"
        "print('RESULT=' + json.dumps({\n"
        "    'loaded': 'mcp' in sys.modules,\n"
        "    'isinstance': isinstance(registry, reloadable_mcp.ReloadableMCPServer),\n"
        "    'tools': tools,\n"
        "    'loop_doc_synced': server.loop.__doc__ == registry._tool_manager.get_tool('loop').fn.__doc__,\n"
        "    'status': state['status'],\n"
        "    'digest_current': state['loaded_digest'] == state['current_digest'],\n"
        "    'resources': len(registry._resource_manager.list_resources()) + len(registry._resource_manager.list_templates()),\n"
        "    'prompts': len(registry._prompt_manager.list_prompts()),\n"
        "}))\n"
    )
    assert result["loaded"] is True
    assert result["isinstance"] is True
    assert len(result["tools"]) >= 100
    assert "loop" in result["tools"]
    assert result["loop_doc_synced"] is True
    assert result["status"] == "current"
    assert result["digest_current"] is True
    assert result["resources"] >= 4
    assert result["prompts"] >= 5


def test_lazy_registry_matches_eager_registry_exactly():
    """Same tools, descriptions and schemas whether built lazily or eagerly."""
    code = (
        "import json, sys\n"
        "if sys.argv[1] == 'eager':\n"
        "    import reloadable_mcp; reloadable_mcp.ReloadableMCPServer\n"
        "import server\n"
        "rows = sorted((t.name, t.description, json.dumps(t.parameters, sort_keys=True))\n"
        "              for t in server.mcp._tool_manager.list_tools())\n"
        "print('RESULT=' + json.dumps(rows))\n"
    )
    assert _run(code, "lazy") == _run(code, "eager")


def test_lazy_staging_commits_on_finish_and_discards_on_abort(tmp_path, monkeypatch):
    import reloadable_mcp

    # In a worker that already imported the SDK the registry would build
    # eagerly; hold it lazy so the recording protocol itself is exercised.
    monkeypatch.setattr(reloadable_mcp, "sdk_loaded", lambda: False)
    source = tmp_path / "sample.py"
    source.write_text("x = 1\n", encoding="utf-8")
    registry = reloadable_mcp.LazyReloadableMCPServer("sample")

    def alpha() -> str:
        return "a"

    def beta() -> str:
        return "b"

    registry.tool()(alpha)
    registry.finish_module_refresh("sample", str(source))
    registry.begin_module_refresh()
    registry.tool()(beta)
    registry.abort_module_refresh(RuntimeError("boom"))
    assert [entry[3] for entry in registry._lazy_committed] == [alpha]
    assert registry._lazy_error == "RuntimeError: source refresh failed"
    registry.begin_module_refresh()
    registry.tool()(beta)
    registry.finish_module_refresh("sample", str(source))
    assert [entry[3] for entry in registry._lazy_committed] == [beta]
    assert registry._lazy_swaps == 1
    assert registry._lazy_error == ""

    # Building replays exactly the committed record, with its bookkeeping.
    real = registry._lazy_materialize()
    assert [tool.name for tool in real._tool_manager.list_tools()] == ["beta"]
    assert real._refresh_count == 1
    assert real._last_error == ""
    assert real._loaded_digest == reloadable_mcp._source_state(source)["digest"]
    assert registry._tool_manager is real._tool_manager
    assert isinstance(registry, reloadable_mcp.ReloadableMCPServer)
