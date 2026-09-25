"""The legacy adaptive_training entrypoint keeps ONE planning implementation.

adaptive_training.py used to carry a full copy of the hardware planner
(dataclasses, budget/estimate helpers, build_plan, formatters) that was
silently shadowed at import time by the re-exports from
``sonder_runtime.application.training.hardware_planning``.  The dead copy
could drift from the live one while still reading as authoritative, so a
fix applied there would never run.  These checks pin the single path.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import adaptive_training
from sonder_runtime.application.training import hardware_planning

SOURCE = Path(adaptive_training.__file__).read_text(encoding="utf-8")
PLANNING_NAMES = (
    "HardwarePlan", "PlanOptions", "Recommendation", "memory_budgets",
    "build_plan", "format_hardware", "format_plan",
)


def _top_level_bindings(tree: ast.Module) -> Counter:
    names: Counter = Counter()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names[node.name] += 1
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names[alias.asname or alias.name] += 1
    return names


def test_planning_names_are_bound_exactly_once():
    bindings = _top_level_bindings(ast.parse(SOURCE))
    duplicated = {name: bindings[name] for name in PLANNING_NAMES if bindings[name] != 1}
    assert duplicated == {}


def test_no_shadowed_legacy_planner_helpers_remain():
    for helper in ("_bounded_available", "_training_estimate",
                   "_inference_estimate", "_requested_size"):
        assert not hasattr(adaptive_training, helper), helper


def test_public_planning_surface_is_unchanged():
    for name in PLANNING_NAMES:
        assert callable(getattr(adaptive_training, name)), name
    assert adaptive_training.PlanOptions is hardware_planning.PlanOptions
    assert adaptive_training.HardwarePlan is hardware_planning.HardwarePlan
    assert adaptive_training.Recommendation is hardware_planning.Recommendation
