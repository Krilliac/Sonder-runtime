"""CI ratchet for legacy stringly ``ERROR:`` producers and parsers."""
from __future__ import annotations

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
