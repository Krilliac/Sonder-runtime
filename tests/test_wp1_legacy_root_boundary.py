"""Focused tests for the single transitional ``server`` import boundary."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.bootstrap.legacy_interfaces import configure_legacy_interfaces
from sonder_runtime.bootstrap.legacy_mcp import build_legacy_server_mcp_runtime
from sonder_runtime.bootstrap.legacy_model import configure_legacy_model_providers

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP = _ROOT / "sonder_runtime" / "bootstrap"


def _imports_server(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(
        isinstance(node, ast.Import)
        and any(alias.name == "server" for alias in node.names)
        for node in ast.walk(tree)
    ) or any(
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "server"
        for node in ast.walk(tree)
    )


def test_legacy_root_is_the_only_production_server_import():
    paths = [
        _BOOTSTRAP / "legacy_root.py",
        _BOOTSTRAP / "legacy_model.py",
        _BOOTSTRAP / "legacy_mcp.py",
        _BOOTSTRAP / "legacy_interfaces.py",
    ]
    assert _imports_server(paths[0])
    assert sum(_imports_server(path) for path in paths) == 1


def test_mcp_accepts_explicit_runtime_and_preserves_hooks():
    events: list[object] = []
    runtime = SimpleNamespace(
        require_mcp_startup_safety=lambda: events.append("safety"),
        run_mcp=lambda *, safety_checked: events.append(("run", safety_checked)),
    )

    adapter = build_legacy_server_mcp_runtime(runtime)
    adapter.require_startup_safety()
    adapter.run(safety_checked=True)

    assert events == ["safety", ("run", True)]


def test_interfaces_pass_the_explicit_runtime_to_both_adapters(monkeypatch):
    calls: list[tuple[str, object]] = []
    runtime = object()

    import sonder_runtime.interfaces.http.serve as serve
    import sonder_runtime.interfaces.repl.repl as repl

    monkeypatch.setattr(
        serve, "configure_legacy_runtime", lambda value: calls.append(("http", value))
    )
    monkeypatch.setattr(
        repl, "configure_legacy_runtime", lambda value: calls.append(("repl", value))
    )
    configure_legacy_interfaces(runtime)

    assert calls == [("http", runtime), ("repl", runtime)]


def test_model_accepts_explicit_runtime(monkeypatch):
    calls: list[tuple[object, ...]] = []
    runtime = SimpleNamespace(
        _serve_target=lambda tier, strict: ("model", False, True, tier),
        _make_generate=lambda *args, **kwargs: calls.append(args) or "ok",
    )
    captured = {}

    monkeypatch.setattr(
        "sonder_runtime.bootstrap.legacy_model.OllamaGateway.configure_default_providers",
        lambda **kwargs: captured.update(kwargs),
    )
    configure_legacy_model_providers(runtime)

    assert captured["target_resolver"]("balanced", True).model == "model"
    assert captured["generate_factory"]("m", "s", 0.1, 2, 3) == "ok"
    assert calls == [("m", "s", 0.1, 2, 3)]


def test_model_provider_extraction_keeps_root_access_in_compatibility_bootstrap():
    source = (_BOOTSTRAP / "legacy_model.py").read_text(encoding="utf-8")
    assert "LegacyModelBootstrapAdapter" in source
    assert "from .legacy_root import runtime as legacy_runtime" in source
    assert source.count("_serve_target") == 0
    assert source.count("_make_generate") == 0
