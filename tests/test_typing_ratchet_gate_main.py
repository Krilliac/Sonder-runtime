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
            "Found 2 errors in 2 files (checked 2 source files)\n"
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
    (tmp_path / "mypy.ini").write_text((REPO_ROOT / "mypy.ini").read_text(encoding="utf-8"), encoding="utf-8")
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
    monkeypatch.setattr(module, "run_mypy", lambda root=tmp_path, expected=1: module.real_run_mypy(root, expected))
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Done())
    assert module.main([]) == 2


def _write_config(tmp_path, extra_global="", sections=""):
    base = (REPO_ROOT / "mypy.ini").read_text(encoding="utf-8")
    path = tmp_path / "mypy.ini"
    path.write_text(base + extra_global + "\n" + sections, encoding="utf-8")
    return path


def test_checked_in_mypy_config_is_valid():
    module = _load_gate()
    assert module.validate_mypy_config(REPO_ROOT / "mypy.ini") == []


@pytest.mark.parametrize(
    ("extra_global", "sections"),
    [
        ("exclude = sonder_runtime/adapters/admin_formatting\\.py\n", ""),
        ("disable_error_code = no-untyped-def\n", ""),
        ("ignore_errors = True\n", ""),
        ("show_absolute_path = True\n", ""),
        ("no_error_summary = True\n", ""),
        ("", "[mypy-sonder_runtime.adapters.admin_formatting]\nignore_errors = True\n"),
        ("", "[mypy-sonder_runtime.*]\nignore_missing_imports = True\n"),
        ("", "[mypy-sonder_*]\nignore_missing_imports = True\n"),
        ("", "[mypy-*]\nignore_missing_imports = True\n"),
        ("", "[mypy-somepkg.*]\nignore_errors = True\n"),
        ("", "[mypy-somepkg]\nignore_missing_imports = False\n"),
    ],
)
def test_mypy_config_that_could_hide_a_strict_module_is_refused(tmp_path, extra_global, sections):
    module = _load_gate()
    assert module.validate_mypy_config(_write_config(tmp_path, extra_global, sections))


def test_third_party_import_section_is_accepted(tmp_path):
    module = _load_gate()
    config = _write_config(tmp_path, "", "[mypy-somepkg.*]\nignore_missing_imports = True\n")
    assert module.validate_mypy_config(config) == []


def test_main_exits_1_when_mypy_config_opts_a_strict_module_out(monkeypatch, tmp_path):
    module, _, _ = _main_env(
        monkeypatch, tmp_path, modules=["m/strict.py"], strict=["m/strict.py"], baseline=[], errors={},
    )
    _write_config(
        tmp_path, "", "[mypy-sonder_runtime.adapters.admin_formatting]\nignore_errors = True\n",
    )
    assert module.main([]) == 1
    assert module.main(["--update"]) == 1


def _fake_mypy(monkeypatch, module, stdout, returncode):
    class Done:
        stderr = ""

    Done.returncode = returncode
    Done.stdout = stdout
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: Done())


def test_mypy_that_checked_fewer_files_than_discovered_is_a_tool_failure(tmp_path, monkeypatch):
    module = _load_gate()
    _fake_mypy(monkeypatch, module, "Success: no issues found in 1 source file\n", 0)
    with pytest.raises(module.RatchetError, match="checked 1 source files"):
        module.run_mypy(tmp_path, 2)


def test_mypy_without_a_summary_line_is_a_tool_failure(tmp_path, monkeypatch):
    module = _load_gate()
    _fake_mypy(monkeypatch, module, "", 0)
    with pytest.raises(module.RatchetError, match="summary"):
        module.run_mypy(tmp_path, 1)


def test_mypy_summary_with_enough_files_is_accepted(tmp_path, monkeypatch):
    module = _load_gate()
    _fake_mypy(
        monkeypatch, module,
        "sonder_runtime/a.py:1: error: x  [misc]\nFound 1 error in 1 file (checked 3 source files)\n", 1,
    )
    assert module.run_mypy(tmp_path, 3) == {"sonder_runtime/a.py": ["sonder_runtime/a.py:1: x  [misc]"]}


def test_error_path_that_is_not_a_discovered_module_is_a_tool_failure():
    module = _load_gate()
    with pytest.raises(module.RatchetError, match="not discovered modules"):
        module.evaluate(MODULES, ["m/strict.py"], ["m/legacy.py"], {"/abs/m/strict.py": ["e"]})


@pytest.mark.parametrize(
    "source",
    [
        "from typing import no_type_check\n\n@no_type_check\ndef f(x):\n    return x\n",
        "import typing\n\n@typing.no_type_check\ndef f(x):\n    return x\n",
        "import typing as t\n\n@t.no_type_check\nclass C:\n    pass\n",
        "from typing import no_type_check as ntc\n",
    ],
)
def test_no_type_check_in_a_strict_module_is_flagged(source):
    assert _load_gate().find_opt_outs(source)


def test_duplicate_mypy_config_key_is_a_tool_failure(tmp_path):
    module = _load_gate()
    with pytest.raises(module.RatchetError, match="cannot read"):
        module.validate_mypy_config(_write_config(tmp_path, "follow_imports = skip\n", ""))
