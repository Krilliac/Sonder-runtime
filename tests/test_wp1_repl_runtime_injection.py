"""WP1 REPL runtime composition and fail-closed access contracts."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.interfaces.repl import repl

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[1]
REPL_PATH = ROOT / "sonder_runtime" / "interfaces" / "repl" / "repl.py"


def test_repl_has_no_direct_server_or_dynamic_import():
    source = REPL_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(REPL_PATH))
    assert "importlib" not in source
    assert "from server import" not in source
    assert not any(
        isinstance(node, ast.Import)
        and any(alias.name == "server" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert not any(
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "server"
        for node in ast.walk(tree)
    )
    assert "server" not in repl.LIVE_RELOAD_MODULES


def test_repl_runtime_access_fails_closed_until_explicitly_configured(monkeypatch):
    monkeypatch.setattr(repl, "_legacy_runtime", None)
    with pytest.raises(DependencyUnavailable, match="configure_legacy_runtime"):
        repl.server.sonder


def test_configured_runtime_preserves_command_calls(monkeypatch):
    calls = []
    runtime = SimpleNamespace(
        permission_mode=lambda **kwargs: calls.append(kwargs) or "mode changed",
    )
    monkeypatch.setattr(repl, "_legacy_runtime", None)
    assert repl.configure_legacy_runtime(runtime) is runtime
    assert repl._mode_command("strict") == "mode changed"
    assert calls == [{"mode": "strict", "explain": False}]


def test_configured_runtime_missing_member_fails_closed(monkeypatch):
    monkeypatch.setattr(repl, "_legacy_runtime", SimpleNamespace())
    with pytest.raises(DependencyUnavailable, match="does not provide sonder"):
        repl.server.sonder
