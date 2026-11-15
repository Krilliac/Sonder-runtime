"""Typing ratchet gate: strict modules may not regress, new modules must be strict."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULES = ["m/strict.py", "m/legacy.py"]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_typing_ratchet_under_test", REPO_ROOT / "scripts" / "check_typing_ratchet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discover_skips_modules_shadowed_by_a_package(tmp_path):
    package = tmp_path / "sonder_runtime"
    (package / "pkg").mkdir(parents=True)
    (package / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (package / "pkg.py").write_text("dead = 1\n", encoding="utf-8")
    (package / "plain.py").write_text("x = 1\n", encoding="utf-8")
    (package / "__pycache__").mkdir()
    (package / "__pycache__" / "junk.py").write_text("", encoding="utf-8")
    assert _load_gate().discover_modules(tmp_path) == [
        "sonder_runtime/pkg/__init__.py", "sonder_runtime/plain.py",
    ]


def test_parse_mypy_output_groups_errors_by_posix_path():
    text = (
        "sonder_runtime\\domain\\a.py:3: error: Missing return  [return]\n"
        "sonder_runtime\\domain\\a.py:3: note: See docs\n"
        "sonder_runtime/domain/b.py:9:5: error: Bad  [misc]\n"
    )
    parsed = _load_gate().parse_mypy_output(text)
    assert sorted(parsed) == ["sonder_runtime/domain/a.py", "sonder_runtime/domain/b.py"]
    assert len(parsed["sonder_runtime/domain/a.py"]) == 1


def test_clean_tree_passes():
    violations, promotable = _load_gate().evaluate(
        MODULES, ["m/strict.py"], ["m/legacy.py"], {"m/legacy.py": ["e"]},
    )
    assert violations == [] and promotable == []


def test_new_untyped_module_fails():
    violations, _ = _load_gate().evaluate(
        [*MODULES, "m/new.py"], ["m/strict.py"], ["m/legacy.py"], {"m/new.py": ["e"]},
    )
    assert any("m/new.py is new" in line for line in violations)


def test_strict_module_gaining_an_error_fails():
    violations, _ = _load_gate().evaluate(
        MODULES, ["m/strict.py"], ["m/legacy.py"], {"m/strict.py": ["m/strict.py:4: oops"]},
    )
    assert violations == ["strict module regressed: m/strict.py:4: oops"]


def test_fixed_baseline_module_is_promotable_not_a_violation():
    violations, promotable = _load_gate().evaluate(MODULES, ["m/strict.py"], ["m/legacy.py"], {})
    assert violations == [] and promotable == ["m/legacy.py"]


def test_stale_and_duplicate_entries_fail():
    violations, _ = _load_gate().evaluate(
        MODULES, ["m/strict.py", "m/gone.py"], ["m/legacy.py", "m/strict.py"], {},
    )
    text = "\n".join(violations)
    assert "m/gone.py, which does not exist" in text
    assert "m/strict.py is in both" in text


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "something unexpected\n"),
        (0, "sonder_runtime/a.py:1: error: x  [misc]\n"),
        (2, ""),
    ],
)
def test_untrustworthy_mypy_results_are_tool_failures(tmp_path, monkeypatch, returncode, stdout):
    module = _load_gate()

    class Done:
        stderr = ""

    Done.returncode = returncode
    Done.stdout = stdout
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Done())
    with pytest.raises(module.RatchetError):
        module.run_mypy(tmp_path)


def test_checked_in_lists_match_the_tree():
    """The list bookkeeping half of the gate, without a mypy run."""
    module = _load_gate()
    strict = module.read_list(module.STRICT_PATH)
    baseline = module.read_list(module.BASELINE_PATH)
    violations, _ = module.evaluate(module.discover_modules(REPO_ROOT), strict, baseline, {})
    assert violations == []
    assert len(strict) == len(set(strict)) and len(baseline) == len(set(baseline))
