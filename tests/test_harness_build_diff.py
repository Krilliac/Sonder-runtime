"""Tests for harness_tools build and diff/patch functions."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import harness_tools


@pytest.fixture(autouse=True)
def _authorize_pytest_tmp_roots(tmp_path_factory, monkeypatch):
    """Authorize pytest's tmp tree for ``harness_tools._resolve_root``.

    ``_resolve_root`` now confines every caller-supplied ``root`` to
    ``file_ops.allowed_roots()`` -- see its docstring for why. These tests
    legitimately do their work in ``tmp_path``, so they authorize exactly that
    tree, the same way an operator authorizes a repository in
    ``file_roots.local``.

    This AUTHORIZES a root; it does not disable the check. A root outside the
    tmp tree is still refused, which is what
    ``tests/test_harness_root_confinement.py`` asserts -- without that file
    this fixture would be indistinguishable from deleting the guard.
    """
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path_factory.getbasetemp()))


def _init_repo(tmp_path):
    """Create a git repo with one committed file."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
    )
    (tmp_path / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "hello.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "-m", "initial"],
        check=True,
    )
    return tmp_path


# ── build_run ───────────────────────────────────────────────────────────


def test_build_run_explicit_command(tmp_path):
    result = harness_tools.build_run(root=str(tmp_path), command="echo built-ok")
    assert result["ok"] is True
    assert "built-ok" in result["stdout"]


@pytest.mark.skipif(
    not shutil.which("make"), reason="make not available",
)
def test_build_run_auto_detect_makefile(tmp_path):
    (tmp_path / "Makefile").write_text(
        "all:\n\t@echo makefile-built\n", encoding="utf-8",
    )
    result = harness_tools.build_run(root=str(tmp_path))
    assert result["ok"] is True
    assert "makefile-built" in result["stdout"]


def test_build_run_no_build_system(tmp_path):
    result = harness_tools.build_run(root=str(tmp_path))
    assert result["ok"] is False
    assert "build system" in result.get("error", "").lower() or "no" in result.get("error", "").lower()


# ── build_clean ─────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not shutil.which("make"), reason="make not available",
)
def test_build_clean_makefile(tmp_path):
    (tmp_path / "Makefile").write_text(
        "clean:\n\t@echo cleaned\n", encoding="utf-8",
    )
    result = harness_tools.build_clean(root=str(tmp_path))
    assert result["ok"] is True
    assert "cleaned" in result["stdout"]


def test_build_clean_no_build_system(tmp_path):
    result = harness_tools.build_clean(root=str(tmp_path))
    assert result["ok"] is False


# ── diff_files ──────────────────────────────────────────────────────────


def test_diff_files_missing_paths_returns_error(tmp_path):
    result = harness_tools.diff_files(root=str(tmp_path), left="", right="b.txt")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_diff_files_missing_file_returns_error(tmp_path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    result = harness_tools.diff_files(root=str(tmp_path), left="a.txt", right="missing.txt")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_diff_files_identical(tmp_path):
    (tmp_path / "a.txt").write_text("same\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("same\n", encoding="utf-8")
    result = harness_tools.diff_files(root=str(tmp_path), left="a.txt", right="b.txt")
    assert result["ok"] is True
    assert result.get("diff", "") == "" or "identical" in str(result).lower()


def test_diff_files_different(tmp_path):
    (tmp_path / "a.txt").write_text("line one\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("line two\n", encoding="utf-8")
    result = harness_tools.diff_files(root=str(tmp_path), left="a.txt", right="b.txt")
    assert "one" in str(result) or "two" in str(result)


# ── apply_patch ─────────────────────────────────────────────────────────


def test_apply_patch_empty_text_returns_error(tmp_path):
    result = harness_tools.apply_patch(root=str(tmp_path), patch_text="")
    assert result["ok"] is False
    assert "required" in result["error"]


def test_apply_patch_check_only(tmp_path):
    root = _init_repo(tmp_path)
    (root / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "hello.txt"],
        capture_output=True, text=True,
    )
    subprocess.run(["git", "-C", str(root), "checkout", "hello.txt"], check=True)
    if diff.stdout.strip():
        result = harness_tools.apply_patch(
            root=str(root), patch_text=diff.stdout, check_only=True,
        )
        assert result["ok"] is True
        assert (root / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def test_apply_patch_applies(tmp_path):
    root = _init_repo(tmp_path)
    (root / "hello.txt").write_text("hello\nworld\n", encoding="utf-8")
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "hello.txt"],
        capture_output=True, text=True,
    )
    subprocess.run(["git", "-C", str(root), "checkout", "hello.txt"], check=True)
    if diff.stdout.strip():
        result = harness_tools.apply_patch(root=str(root), patch_text=diff.stdout)
        assert result["ok"] is True
        assert "world" in (root / "hello.txt").read_text(encoding="utf-8")
