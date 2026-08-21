"""WP1 evidence for the packaged agent-registry composition boundary."""
from __future__ import annotations

import ast
from pathlib import Path

from sonder_runtime.adapters.runtime_capabilities import RuntimeCapabilities
from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
from sonder_runtime.adapters import runtime_container


ROOT = Path(__file__).resolve().parents[1]


def _imports_root_master(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.Import)
        and any(alias.name == "master_orchestrator" for alias in node.names)
        for node in ast.walk(tree)
    ) or any(
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "master_orchestrator"
        for node in ast.walk(tree)
    )


def test_packaged_runtime_has_no_root_master_orchestrator_import():
    packaged = ROOT / "sonder_runtime"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in packaged.rglob("*.py")
        if _imports_root_master(path)
    ]
    assert offenders == []


def test_packaged_runtime_composes_agent_registry_lazily(monkeypatch):
    calls = []

    class FakeRegistry:
        def register_workbench_modes(self):
            calls.append("register")

    class FakeAdapter:
        def __init__(self):
            calls.append("adapter")

    monkeypatch.setattr(runtime_container, "FleetStoreRegistryAdapter", FakeAdapter)
    monkeypatch.setattr(
        runtime_container,
        "UnifiedAgentRegistryService",
        lambda adapter: calls.append(adapter.__class__.__name__) or FakeRegistry(),
    )

    runtime = runtime_container.build_runtime(
        RuntimeConfig(), RuntimeCapabilities()
    )
    assert calls == []
    assert runtime.agent_registry() is runtime.agent_registry()
    assert calls == ["adapter", "FakeAdapter", "register"]
