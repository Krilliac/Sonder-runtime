"""WP1 command-surface root-removal contract tests."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sonder_runtime.adapters.command_surface import LegacyServerMcpRuntime
from sonder_runtime.domain.common.errors import DependencyUnavailable


ROOT = Path(__file__).parents[1]


def _has_direct_server_import(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        (isinstance(node, ast.Import) and any(alias.name == "server" for alias in node.names))
        or (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module == "server"
        )
        for node in ast.walk(tree)
    )


def test_command_surface_adapter_has_no_root_or_dynamic_import_bypass():
    path = ROOT / "sonder_runtime" / "adapters" / "command_surface.py"
    source = path.read_text(encoding="utf-8")

    assert not _has_direct_server_import(path)
    assert "importlib" not in source
    assert "__import__(" not in source


def test_injected_hooks_preserve_safety_then_run_order():
    events: list[object] = []

    runtime = LegacyServerMcpRuntime(
        require_startup_safety=lambda: events.append("safety"),
        run_mcp=lambda *, safety_checked: events.append(("run", safety_checked)),
    )

    runtime.require_startup_safety()
    runtime.run(safety_checked=True)

    assert events == ["safety", ("run", True)]


def test_missing_startup_hook_fails_closed_before_run():
    events: list[str] = []
    runtime = LegacyServerMcpRuntime(
        run_mcp=lambda **_: events.append("run"),
    )

    with pytest.raises(DependencyUnavailable, match="startup-safety"):
        runtime.require_startup_safety()
    assert events == []


def test_missing_run_hook_fails_closed_after_safety():
    events: list[str] = []
    runtime = LegacyServerMcpRuntime(
        require_startup_safety=lambda: events.append("safety"),
    )

    runtime.require_startup_safety()
    with pytest.raises(DependencyUnavailable, match="injected run hook"):
        runtime.run(safety_checked=True)
    assert events == ["safety"]


def test_entrypoint_uses_bounded_legacy_composition_factory():
    path = ROOT / "sonder_runtime" / "__main__.py"
    source = path.read_text(encoding="utf-8")

    assert "build_legacy_server_mcp_runtime" in source
    assert "LegacyServerMcpRuntime(" not in source

