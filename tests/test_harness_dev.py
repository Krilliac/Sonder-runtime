"""Tests for harness_tools developer workflow functions (test/lint/format/typecheck)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import harness_tools


# ---------------------------------------------------------------------------
# _detect_test_framework
# ---------------------------------------------------------------------------


def test_detect_test_framework_conftest(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "pytest"


def test_detect_test_framework_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "pytest"


def test_detect_test_framework_jest_config(tmp_path):
    (tmp_path / "jest.config.js").write_text("", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "jest"


def test_detect_test_framework_vitest_config(tmp_path):
    (tmp_path / "vitest.config.ts").write_text("", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "vitest"


def test_detect_test_framework_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "cargo"


def test_detect_test_framework_go(tmp_path):
    (tmp_path / "go.mod").write_text("module example\n", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "go"


def test_detect_test_framework_dotnet_glob(tmp_path):
    (tmp_path / "MyApp.csproj").write_text("<Project/>\n", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "dotnet"


def test_detect_test_framework_package_json_jest(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest --ci"}}), encoding="utf-8",
    )
    assert harness_tools._detect_test_framework(tmp_path) == "jest"


def test_detect_test_framework_package_json_vitest(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8",
    )
    assert harness_tools._detect_test_framework(tmp_path) == "vitest"


def test_detect_test_framework_empty_dir_defaults_pytest(tmp_path):
    assert harness_tools._detect_test_framework(tmp_path) == "pytest"


def test_detect_test_framework_bad_package_json(tmp_path):
    (tmp_path / "package.json").write_text("{bad json", encoding="utf-8")
    assert harness_tools._detect_test_framework(tmp_path) == "pytest"


# ---------------------------------------------------------------------------
# _detect_linter
# ---------------------------------------------------------------------------


def test_detect_linter_ruff_with_pyproject(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    assert harness_tools._detect_linter(tmp_path) == "ruff"


def test_detect_linter_eslint(tmp_path, monkeypatch):
    (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness_tools._detect_linter(tmp_path) == "eslint"


def test_detect_linter_eslint_flat_config(tmp_path, monkeypatch):
    (tmp_path / "eslint.config.js").write_text("", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness_tools._detect_linter(tmp_path) == "eslint"


def test_detect_linter_clippy(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness_tools._detect_linter(tmp_path) == "clippy"


def test_detect_linter_fallback_ruff(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    assert harness_tools._detect_linter(tmp_path) == "ruff"


# ---------------------------------------------------------------------------
# _detect_formatter
# ---------------------------------------------------------------------------


def test_detect_formatter_ruff_with_pyproject(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ruff" if name == "ruff" else None)
    assert harness_tools._detect_formatter(tmp_path) == "ruff"


def test_detect_formatter_prettier_via_package_json(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness_tools._detect_formatter(tmp_path) == "prettier"


def test_detect_formatter_rustfmt(tmp_path, monkeypatch):
    (tmp_path / "Cargo.toml").write_text("[package]", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness_tools._detect_formatter(tmp_path) == "rustfmt"


def test_detect_formatter_gofmt(tmp_path, monkeypatch):
    (tmp_path / "go.mod").write_text("module example", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness_tools._detect_formatter(tmp_path) == "gofmt"


# ---------------------------------------------------------------------------
# _detect_typechecker
# ---------------------------------------------------------------------------


def test_detect_typechecker_tsc(tmp_path):
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
    assert harness_tools._detect_typechecker(tmp_path) == "tsc"


def test_detect_typechecker_pyright(tmp_path):
    (tmp_path / "pyrightconfig.json").write_text("{}", encoding="utf-8")
    assert harness_tools._detect_typechecker(tmp_path) == "pyright"


def test_detect_typechecker_mypy_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/mypy" if name == "mypy" else None)
    assert harness_tools._detect_typechecker(tmp_path) == "mypy"


def test_detect_typechecker_default_mypy(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert harness_tools._detect_typechecker(tmp_path) == "mypy"


# ---------------------------------------------------------------------------
# test_discover
# ---------------------------------------------------------------------------


def test_discover_pytest_autodetect(tmp_path):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    (tmp_path / "test_sample.py").write_text(
        "def test_one(): pass\ndef test_two(): pass\n", encoding="utf-8",
    )
    result = harness_tools.test_discover(root=str(tmp_path), framework="auto")
    assert result["framework"] == "pytest"
    assert result["test_count"] == 2
    assert any("test_sample.py" in f for f in result["test_files"])


def test_discover_explicit_framework(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    result = harness_tools.test_discover(root=str(tmp_path), framework="pytest")
    assert result["framework"] == "pytest"
    assert result["test_count"] >= 1


def test_discover_unknown_framework_returns_empty(tmp_path):
    result = harness_tools.test_discover(root=str(tmp_path), framework="unknown_fw")
    assert result["framework"] == "unknown_fw"
    assert result["test_count"] == 0


# ---------------------------------------------------------------------------
# test_run
# ---------------------------------------------------------------------------


def test_run_pytest_passing(tmp_path):
    (tmp_path / "test_trivial.py").write_text(
        "def test_pass(): assert True\ndef test_fail(): assert False\n",
        encoding="utf-8",
    )
    result = harness_tools.test_run(
        root=str(tmp_path), framework="pytest", pattern="test_pass", timeout=30,
    )
    assert result["framework"] == "pytest"
    assert result["ok"] is True
    assert result["returncode"] == 0


def test_run_pytest_failing(tmp_path):
    (tmp_path / "test_trivial.py").write_text(
        "def test_fail(): assert False\n", encoding="utf-8",
    )
    result = harness_tools.test_run(root=str(tmp_path), framework="pytest", timeout=30)
    assert result["ok"] is False
    assert result["returncode"] != 0


def test_run_pytest_with_path(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "test_sub.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    result = harness_tools.test_run(
        root=str(tmp_path), framework="pytest", path=str(sub / "test_sub.py"), timeout=30,
    )
    assert result["ok"] is True


def test_run_unknown_framework():
    result = harness_tools.test_run(root=".", framework="bogus_framework", timeout=5)
    assert result["ok"] is False
    assert "unknown framework" in result.get("error", "")
    assert result["framework"] == "bogus_framework"


def test_run_extra_args(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    result = harness_tools.test_run(
        root=str(tmp_path), framework="pytest",
        extra_args_json='["--co", "-q"]', timeout=30,
    )
    assert result["framework"] == "pytest"
    assert result["ok"] is True


def test_run_bad_extra_args_json(tmp_path):
    (tmp_path / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    result = harness_tools.test_run(
        root=str(tmp_path), framework="pytest",
        extra_args_json="{bad json", timeout=30,
    )
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# lint_run
# ---------------------------------------------------------------------------


def test_lint_run_unknown_tool():
    result = harness_tools.lint_run(root=".", tool="nonexistent_linter", timeout=5)
    assert result["ok"] is False
    assert "unknown linter" in result["error"]
    assert result["tool"] == "nonexistent_linter"


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_lint_run_ruff_check_mode(tmp_path):
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    result = harness_tools.lint_run(root=str(tmp_path), tool="ruff", path="clean.py", fix=False, timeout=30)
    assert result["tool"] == "ruff"
    assert result["mode"] == "check"
    assert "command" in result


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_lint_run_ruff_fix_mode(tmp_path):
    (tmp_path / "fixable.py").write_text("import os\nimport sys\nx = 1\n", encoding="utf-8")
    result = harness_tools.lint_run(root=str(tmp_path), tool="ruff", path="fixable.py", fix=True, timeout=30)
    assert result["tool"] == "ruff"
    assert result["mode"] == "fix"


# ---------------------------------------------------------------------------
# format_code
# ---------------------------------------------------------------------------


def test_format_code_unknown_tool():
    result = harness_tools.format_code(root=".", tool="nonexistent_formatter", timeout=5)
    assert result["ok"] is False
    assert "unknown formatter" in result["error"]
    assert result["tool"] == "nonexistent_formatter"


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_format_code_ruff_check_only(tmp_path):
    (tmp_path / "formatted.py").write_text("x = 1\n", encoding="utf-8")
    result = harness_tools.format_code(
        root=str(tmp_path), tool="ruff", path="formatted.py", check_only=True, timeout=30,
    )
    assert result["tool"] == "ruff"
    assert result["mode"] == "check"


@pytest.mark.skipif(not shutil.which("ruff"), reason="ruff not installed")
def test_format_code_ruff_write(tmp_path):
    (tmp_path / "messy.py").write_text("x=1\n", encoding="utf-8")
    result = harness_tools.format_code(
        root=str(tmp_path), tool="ruff", path="messy.py", check_only=False, timeout=30,
    )
    assert result["tool"] == "ruff"
    assert result["mode"] == "format"


# ---------------------------------------------------------------------------
# typecheck_run
# ---------------------------------------------------------------------------


def test_typecheck_run_unknown_tool():
    result = harness_tools.typecheck_run(root=".", tool="nonexistent_checker", timeout=5)
    assert result["ok"] is False
    assert "unknown type checker" in result["error"]
    assert result["tool"] == "nonexistent_checker"


@pytest.mark.skipif(not shutil.which("mypy"), reason="mypy not installed")
def test_typecheck_run_mypy(tmp_path):
    (tmp_path / "typed.py").write_text("x: int = 1\n", encoding="utf-8")
    result = harness_tools.typecheck_run(root=str(tmp_path), tool="mypy", path="typed.py", timeout=60)
    assert result["tool"] == "mypy"
    assert "command" in result


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def test_bounded_int_normal():
    assert harness_tools._bounded_int(50, 30, 5, 120) == 50


def test_bounded_int_clamp_low():
    assert harness_tools._bounded_int(1, 30, 5, 120) == 5


def test_bounded_int_clamp_high():
    assert harness_tools._bounded_int(999, 30, 5, 120) == 120


def test_bounded_int_default_on_bad_value():
    assert harness_tools._bounded_int("abc", 30, 5, 120) == 30
    assert harness_tools._bounded_int(None, 30, 5, 120) == 30


def test_trim_short_string():
    assert harness_tools._trim("hello", 100) == "hello"


def test_trim_long_string():
    text = "x" * 1000
    result = harness_tools._trim(text, 100)
    assert len(result) < 1000
    assert "trimmed" in result


def test_resolve_root_valid(tmp_path):
    resolved = harness_tools._resolve_root(str(tmp_path))
    assert resolved == tmp_path.resolve()


def test_resolve_root_invalid():
    with pytest.raises(ValueError, match="not a directory"):
        harness_tools._resolve_root("/nonexistent/path/abc123xyz")
