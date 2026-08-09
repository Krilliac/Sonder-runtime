"""CI ratchet for legacy stringly ``ERROR:`` producers and parsers."""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys

import pytest


pytestmark = pytest.mark.unit
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKER_PATH = _REPO_ROOT / "scripts" / "check_error_signals.py"
_SPEC = importlib.util.spec_from_file_location("check_error_signals", _CHECKER_PATH)
checker = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = checker
_SPEC.loader.exec_module(checker)


def _write(root: Path, name: str, source: str) -> None:
    (root / name).write_text(source, encoding="utf-8")


def test_checked_in_error_signal_universe_has_no_growth_or_swaps():
    assert checker.check() == []


def test_signal_fingerprint_does_not_depend_on_ast_dump_defaults(monkeypatch):
    finding = checker.Finding(
        "producer.py", "produce", checker.CATEGORY_RETURN, 1,
        '"ERROR: {}".format(value)',
    )
    expected = finding.key
    monkeypatch.setattr(checker.ast, "dump", lambda *args, **kwargs: "changed")

    assert finding.key == expected
    normalized = checker._stable_ast_dump(ast.parse("call()", mode="eval"))
    assert "args=[]" in normalized
    assert "keywords=[]" in normalized


def test_error_signal_addition_fails_with_actionable_diagnostic(tmp_path):
    _write(tmp_path, "alpha.py", 'def old():\n    return "ERROR: old"\n')
    baseline = checker.baseline_from(checker.inventory(tmp_path))
    allowed = {
        (row["path"], row["scope"], row["category"], row["signal"]): row["max_count"]
        for row in baseline["entries"]
    }
    _write(
        tmp_path,
        "alpha.py",
        'def old():\n    return "ERROR: old"\n\n'
        'def added():\n    return f"ERROR: {1}"\n',
    )

    problems = checker.violations(checker.inventory(tmp_path), allowed)

    assert len(problems) == 1
    assert "alpha.py:5" in problems[0]
    assert "return_literal_prefix in added" in problems[0]
    assert "baseline allows 0" in problems[0]


def test_error_signal_swap_fails_even_when_total_count_is_unchanged(tmp_path):
    _write(tmp_path, "alpha.py", 'def old():\n    return "ERROR: old"\n')
    baseline_payload = checker.baseline_from(checker.inventory(tmp_path))
    baseline = {
        (row["path"], row["scope"], row["category"], row["signal"]): row["max_count"]
        for row in baseline_payload["entries"]
    }
    _write(tmp_path, "alpha.py", 'def old():\n    return "ERROR: replacement"\n')

    problems = checker.violations(checker.inventory(tmp_path), baseline)

    assert len(problems) == 1
    assert "alpha.py:2" in problems[0]
    assert "return_literal_prefix in old" in problems[0]
    assert "replacement" in problems[0]


def test_error_signal_removal_passes_shrink_only_ratchet(tmp_path):
    _write(tmp_path, "alpha.py", 'def old():\n    return "ERROR: old"\n')
    baseline_payload = checker.baseline_from(checker.inventory(tmp_path))
    baseline = {
        (row["path"], row["scope"], row["category"], row["signal"]): row["max_count"]
        for row in baseline_payload["entries"]
    }
    _write(tmp_path, "alpha.py", 'def old():\n    return "typed failure"\n')

    assert checker.violations(checker.inventory(tmp_path), baseline) == []


def test_new_error_prefix_parser_is_also_rejected(tmp_path):
    baseline = {}
    _write(
        tmp_path,
        "consumer.py",
        'def parse(value):\n    return value.startswith(("OK:", "ERROR:"))\n',
    )

    problems = checker.violations(checker.inventory(tmp_path), baseline)

    assert len(problems) == 1
    assert "startswith_parser in parse" in problems[0]


@pytest.mark.parametrize(
    "expression",
    [
        'str.startswith(value, "ERROR:")',
        'value.startswith(("OK:",) + ("ERROR:",))',
        'value.startswith("ERROR" + ":")',
        'value.startswith("ERROR:" * 1)',
    ],
)
def test_equivalent_startswith_parser_variants_cannot_bypass_ratchet(
    tmp_path, expression
):
    _write(tmp_path, "consumer.py", f"def parse(value):\n    return {expression}\n")

    problems = checker.violations(checker.inventory(tmp_path), {})

    assert len(problems) == 1
    assert "startswith_parser in parse" in problems[0]


def test_path_rename_is_growth_even_when_total_and_signal_are_unchanged(
    tmp_path,
):
    _write(tmp_path, "alpha.py", 'def old():\n    return "ERROR: old"\n')
    baseline_payload = checker.baseline_from(checker.inventory(tmp_path))
    baseline = {
        (row["path"], row["scope"], row["category"], row["signal"]):
        row["max_count"]
        for row in baseline_payload["entries"]
    }
    (tmp_path / "alpha.py").rename(tmp_path / "renamed.py")

    problems = checker.violations(checker.inventory(tmp_path), baseline)

    assert len(problems) == 1
    assert "renamed.py:2" in problems[0]


@pytest.mark.parametrize(
    "expression",
    [
        'f"ERROR: {value}"',
        '"ERROR: " + value',
        '"ERROR: %s" % value',
        '"ERROR: {}".format(value)',
        '"ERROR: {value}".format_map({"value": value})',
        '"ok" if value else "ERROR: fallback"',
    ],
)
def test_literal_derived_return_shapes_are_in_scope(tmp_path, expression):
    _write(tmp_path, "producer.py", f"def produce(value):\n    return {expression}\n")

    problems = checker.violations(checker.inventory(tmp_path), {})

    assert len(problems) == 1
    assert "return_literal_prefix in produce" in problems[0]


@pytest.mark.parametrize(
    "expression",
    [
        '"ERROR" + ":"',
        '"ERROR:" * 1',
        '("ERROR" + ":") + value',
    ],
)
def test_static_return_prefix_composition_cannot_bypass_ratchet(
    tmp_path, expression,
):
    _write(tmp_path, "producer.py", f"def produce(value):\n    return {expression}\n")

    problems = checker.violations(checker.inventory(tmp_path), {})

    assert len(problems) == 1
    assert "return_literal_prefix in produce" in problems[0]
