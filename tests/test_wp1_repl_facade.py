"""WP1 REPL route-family facade contracts."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sonder_runtime.interfaces.repl.facades import (
    ContextHealthFacade,
    ExecutionStatusFacade,
    InstalledModel,
    ModelSelectionFacade,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).parents[1]
FACADE_ROOT = ROOT / "sonder_runtime" / "interfaces" / "repl" / "facades"


def test_facade_modules_are_root_free_and_do_not_use_dynamic_imports():
    for path in FACADE_ROOT.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        assert "importlib" not in source
        assert "__import__(" not in source
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


def test_model_catalog_parser_distinguishes_unavailable_from_empty():
    assert ModelSelectionFacade.installed_models({"models": []}) == ()
    assert ModelSelectionFacade.installed_models({"unexpected": []}) is None
    assert ModelSelectionFacade.installed_models({"models": [
        {"name": "qwen:30b", "size": 2 * 1024 ** 3},
        {"model": "tiny", "size": "bad"},
        {"name": ""},
        "ignore",
    ]}) == (
        InstalledModel("qwen:30b", "2.0 GB"),
        InstalledModel("tiny", ""),
    )


def test_model_selection_policy_and_choices_are_deterministic():
    assert ModelSelectionFacade.resolved_model({"code": "qwen"}, "code") == "qwen"
    assert ModelSelectionFacade.resolved_model({}, "code") == "unknown"
    assert ModelSelectionFacade.resolved_model({}, "code", "pinned") == "pinned"
    available = ModelSelectionFacade.selectable_tiers(
        {"code": "qwen", "fast": "small"},
        {"code": "qwen", "cloud": "hosted"},
    )
    assert available == {"code": "qwen", "fast": "small"}
    assert ModelSelectionFacade.selectable_tiers(None, {"code": "qwen"}) == {
        "code": "qwen"
    }
    assert ModelSelectionFacade.withheld_reason(
        "cloud", configured={"cloud": "hosted"}, available={},
        cloud_tiers=("cloud",), cloud_disabled_message="ERROR: cloud disabled",
    ) == "cloud disabled"
    assert ModelSelectionFacade.withheld_reason(
        "unknown", configured={"cloud": "hosted"}, available={},
    ) == ""
    assert ModelSelectionFacade.choices(
        available,
        (InstalledModel("zeta"), InstalledModel("code")),
    ) == ["code", "fast", "zeta"]


def test_status_and_context_facades_fail_closed_and_normalize_counts():
    status = ExecutionStatusFacade(
        lambda: {"known": True, "running_lanes": 2, "running_agents": 3, "queued_agents": 1}
    )
    assert status.prompt_text() == "[lanes 2 | agents 3+1q]"
    assert status.counts() == (2, 3)

    def broken_status():
        raise RuntimeError("unavailable")

    assert ExecutionStatusFacade(broken_status).prompt_text() == "[lanes ? | agents ?]"
    context = ContextHealthFacade(
        lambda **_: {"estimated_tokens": 1200, "context_limit": 2000}
    )
    assert context.snapshot("session", "project") == {
        "used": 1200, "limit": 2000, "left": 800,
    }

    def broken_context(**_):
        raise RuntimeError("unavailable")

    assert ContextHealthFacade(broken_context).snapshot("session", "project") is None
