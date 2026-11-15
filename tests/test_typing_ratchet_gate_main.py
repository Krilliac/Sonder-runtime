"""Typing ratchet gate: opt-out scan, unreadable mypy output and main()/--update behaviour."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULES = ["m/strict.py", "m/legacy.py"]


def _load_gate():
    spec = importlib.util.spec_from_file_location(
        "check_typing_ratchet_main_under_test", REPO_ROOT / "scripts" / "check_typing_ratchet.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "source",
    [
        "# mypy: ignore-errors\nx = 1\n",
        "x = 1  # mypy: allow-untyped-defs\n",
        "# mypy: disallow-untyped-defs=False\nx = 1\n",
        "# type: ignore\nx = 1\n",
        '"""Doc."""\n# type: ignore[misc]\nx = 1\n',
    ],
)
def test_strict_module_opting_itself_out_is_flagged(source):
    assert _load_gate().find_opt_outs(source)


@pytest.mark.parametrize(
    "source",
    [
        "x = 1\ny = 2  # type: ignore[assignment]\n",
        '"""Doc mentioning # mypy: ignore-errors."""\nx = "# type: ignore"\n',
        "x = 1\n# type: ignore-ish remark\n",
    ],
)
def test_ordinary_code_is_not_flagged_as_opt_out(source):
    assert _load_gate().find_opt_outs(source) == []


def test_evaluate_reports_opt_out_only_for_strict_modules(tmp_path):
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "strict.py").write_text("# mypy: ignore-errors\nx = 1\n", encoding="utf-8")
    (tmp_path / "m" / "legacy.py").write_text("# mypy: ignore-errors\nx = 1\n", encoding="utf-8")
    violations, _ = _load_gate().evaluate(MODULES, ["m/strict.py"], ["m/legacy.py"], {}, tmp_path)
    assert len(violations) == 1
    assert "opts out of checking: m/strict.py" in violations[0]


def test_checked_in_strict_modules_do_not_opt_out():
    module = _load_gate()
    violations, _ = module.evaluate(
        module.discover_modules(REPO_ROOT), module.read_list(module.STRICT_PATH),
        module.read_list(module.BASELINE_PATH), {}, REPO_ROOT,
    )
    assert violations == []


def test_error_line_that_cannot_be_attributed_is_a_tool_failure(tmp_path, monkeypatch):
    module = _load_gate()

    class Done:
        returncode = 1
        stderr = ""
        stdout = (
            "sonder_runtime/a.py:1: error: x  [misc]\n"
            "C:\\work\\sonder_runtime\\b.py:2: error: y  [misc]\n"
        )

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Done())
    with pytest.raises(module.RatchetError, match="readable"):
        module.run_mypy(tmp_path)


def _main_env(monkeypatch, tmp_path, *, modules, strict, baseline, errors):
    """Point main() at a throwaway tree with fixed mypy results."""
    module = _load_gate()
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    strict_path = scripts / "typing_strict_modules.txt"
    baseline_path = scripts / "typing_baseline.txt"
    strict_path.write_text("".join(p + "\n" for p in strict), encoding="utf-8")
    baseline_path.write_text("".join(p + "\n" for p in baseline), encoding="utf-8")
    for path in modules:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n", encoding="utf-8")
    module.real_run_mypy = module.run_mypy
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "STRICT_PATH", strict_path)
    monkeypatch.setattr(module, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(module, "discover_modules", lambda *args, **kwargs: list(modules))
    monkeypatch.setattr(module, "run_mypy", lambda *args, **kwargs: dict(errors))
    return module, strict_path, baseline_path


def test_update_promotes_fixed_modules_drops_deleted_and_never_grows_baseline(monkeypatch, tmp_path):
    modules = ["m/strict.py", "m/legacy.py", "m/fixed.py", "m/new_clean.py"]
    module, strict_path, baseline_path = _main_env(
        monkeypatch, tmp_path, modules=modules, strict=["m/strict.py", "m/gone.py"],
        baseline=["m/legacy.py", "m/fixed.py", "m/gone2.py"], errors={"m/legacy.py": ["e"]},
    )
    assert module.main(["--update"]) == 0
    assert module.read_list(strict_path) == ["m/fixed.py", "m/new_clean.py", "m/strict.py"]
    assert module.read_list(baseline_path) == ["m/legacy.py"]


def test_update_leaves_a_new_failing_file_out_of_both_lists_and_exits_1(monkeypatch, tmp_path):
    modules = ["m/strict.py", "m/legacy.py", "m/new_bad.py"]
    module, strict_path, baseline_path = _main_env(
        monkeypatch, tmp_path, modules=modules, strict=["m/strict.py"], baseline=["m/legacy.py"],
        errors={"m/legacy.py": ["e"], "m/new_bad.py": ["e"]},
    )
    assert module.main(["--update"]) == 1
    assert "m/new_bad.py" not in module.read_list(baseline_path)
    assert "m/new_bad.py" not in module.read_list(strict_path)


def test_update_refuses_while_a_strict_module_has_errors(monkeypatch, tmp_path):
    module, strict_path, baseline_path = _main_env(
        monkeypatch, tmp_path, modules=["m/strict.py", "m/fixed.py"], strict=["m/strict.py"],
        baseline=["m/fixed.py"], errors={"m/strict.py": ["m/strict.py:1: oops"]},
    )
    before = (strict_path.read_text(encoding="utf-8"), baseline_path.read_text(encoding="utf-8"))
    assert module.main(["--update"]) == 1
    assert (strict_path.read_text(encoding="utf-8"), baseline_path.read_text(encoding="utf-8")) == before


def test_update_refuses_while_a_strict_module_opts_out(monkeypatch, tmp_path):
    module, _, _ = _main_env(
        monkeypatch, tmp_path, modules=["m/strict.py"], strict=["m/strict.py"], baseline=[], errors={},
    )
    (tmp_path / "m" / "strict.py").write_text("# mypy: ignore-errors\nx = 1\n", encoding="utf-8")
    assert module.main(["--update"]) == 1
    assert module.main([]) == 1


def test_main_exits_1_for_a_new_failing_file(monkeypatch, tmp_path):
    module, _, _ = _main_env(
        monkeypatch, tmp_path, modules=["m/strict.py", "m/new_bad.py"], strict=["m/strict.py"],
        baseline=[], errors={"m/new_bad.py": ["e"]},
    )
    assert module.main([]) == 1


def test_main_exits_0_on_a_clean_tree(monkeypatch, tmp_path):
    module, _, _ = _main_env(
        monkeypatch, tmp_path, modules=["m/strict.py", "m/legacy.py"], strict=["m/strict.py"],
        baseline=["m/legacy.py"], errors={"m/legacy.py": ["e"]},
    )
    assert module.main([]) == 0


def test_main_exits_2_for_an_empty_strict_list(monkeypatch, tmp_path):
    module, _, _ = _main_env(
        monkeypatch, tmp_path, modules=["m/legacy.py"], strict=[], baseline=["m/legacy.py"], errors={},
    )
    assert module.main([]) == 2


def test_main_exits_2_when_mypy_is_missing(monkeypatch, tmp_path):
    module, _, _ = _main_env(
        monkeypatch, tmp_path, modules=["m/strict.py"], strict=["m/strict.py"], baseline=[], errors={},
    )

    class Done:
        returncode = 1
        stdout = ""
        stderr = "python: No module named mypy"

    # The helper stubs run_mypy; put the real one back and fake only the subprocess.
    monkeypatch.setattr(module, "run_mypy", lambda root=tmp_path: module.real_run_mypy(root))
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Done())
    assert module.main([]) == 2
