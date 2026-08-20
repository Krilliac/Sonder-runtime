"""WP1: the packaged entrypoint must not import the legacy server root."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sonder_runtime.application.command_surface import McpCommand

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def test_main_has_no_direct_server_import_or_dynamic_import_bypass():
    path = _ROOT / "sonder_runtime" / "__main__.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    direct_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        and any(alias.name == "server" for alias in node.names)
    ]
    from_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "server"
    ]

    assert not direct_imports
    assert not from_imports
    assert "importlib" not in source
    assert "__import__(" not in source


def test_mcp_command_preserves_safety_configure_run_order():
    events: list[str] = []

    class Runtime:
        def require_startup_safety(self):
            events.append("safety")

        def run(self, *, safety_checked):
            events.append(f"run:{safety_checked}")

    McpCommand(Runtime()).execute(lambda: events.append("configure"))

    assert events == ["safety", "configure", "run:True"]


def test_mcp_command_does_not_run_after_configuration_failure():
    events: list[str] = []

    class Runtime:
        def require_startup_safety(self):
            events.append("safety")

        def run(self, *, safety_checked):
            events.append("run")

    def fail_configuration():
        events.append("configure")
        raise RuntimeError("invalid configuration")

    with pytest.raises(RuntimeError, match="invalid configuration"):
        McpCommand(Runtime()).execute(fail_configuration)

    assert events == ["safety", "configure"]


def test_legacy_adapter_delegates_existing_server_hooks(monkeypatch):
    import server

    from sonder_runtime.adapters.command_surface import LegacyServerMcpRuntime

    events: list[object] = []
    monkeypatch.setattr(
        server,
        "require_mcp_startup_safety",
        lambda: events.append("safety"),
    )
    monkeypatch.setattr(
        server,
        "run_mcp",
        lambda *, safety_checked: events.append(("run", safety_checked)),
    )

    runtime = LegacyServerMcpRuntime(
        require_startup_safety=server.require_mcp_startup_safety,
        run_mcp=server.run_mcp,
    )
    runtime.require_startup_safety()
    runtime.run(safety_checked=True)

    assert events == ["safety", ("run", True)]
