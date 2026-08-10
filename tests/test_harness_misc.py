"""Tests for harness_tools: dependency, refactoring, build, diff/patch, and security."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import harness_tools


class TestDetectPackageManager:
    def test_requirements_txt(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "pip"

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "pip"

    def test_package_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "npm"

    def test_pnpm_lock(self, tmp_path):
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 5\n", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "pnpm"

    def test_yarn_lock(self, tmp_path):
        (tmp_path / "yarn.lock").write_text("# yarn lockfile\n", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "yarn"

    def test_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "cargo"

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example\n", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "go"

    def test_gemfile(self, tmp_path):
        (tmp_path / "Gemfile").write_text("source 'https://rubygems.org'\n", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "bundler"

    def test_empty_directory_defaults_to_pip(self, tmp_path):
        assert harness_tools._detect_package_manager(tmp_path) == "pip"

    def test_cargo_wins_over_package_json(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        (tmp_path / "package.json").write_text("{}", encoding="utf-8")
        assert harness_tools._detect_package_manager(tmp_path) == "cargo"


class TestDependencyAdd:
    def test_invalid_json(self, tmp_path):
        result = harness_tools.dependency_add(root=str(tmp_path), packages_json="not json")
        assert result["ok"] is False
        assert "invalid JSON" in result["error"]

    def test_empty_packages(self, tmp_path):
        result = harness_tools.dependency_add(root=str(tmp_path), packages_json="[]")
        assert result["ok"] is False
        assert "non-empty" in result["error"]

    def test_non_array_packages(self, tmp_path):
        result = harness_tools.dependency_add(root=str(tmp_path), packages_json='"requests"')
        assert result["ok"] is False
        assert "non-empty" in result["error"]


class TestDependencyRemove:
    def test_invalid_json(self, tmp_path):
        result = harness_tools.dependency_remove(root=str(tmp_path), packages_json="{bad}")
        assert result["ok"] is False
        assert "invalid JSON" in result["error"]

    def test_empty_packages(self, tmp_path):
        result = harness_tools.dependency_remove(root=str(tmp_path), packages_json="[]")
        assert result["ok"] is False
        assert "non-empty" in result["error"]


class TestDependencyUpdate:
    def test_pip_detection(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
        result = harness_tools.dependency_update(root=str(tmp_path), packages_json='["requests"]')
        assert result.get("manager") == "pip"

    def test_npm_detection(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"test"}', encoding="utf-8")
        result = harness_tools.dependency_update(root=str(tmp_path))
        assert result.get("manager") == "npm"

    def test_cargo_detection(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        result = harness_tools.dependency_update(root=str(tmp_path))
        assert result.get("manager") == "cargo"


class TestDependencyAudit:
    def test_pip_detection(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
        result = harness_tools.dependency_audit(root=str(tmp_path))
        assert result.get("manager") == "pip"

    def test_npm_detection(self, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"test"}', encoding="utf-8")
        result = harness_tools.dependency_audit(root=str(tmp_path))
        assert result.get("manager") == "npm"


class TestRenameSymbol:
    def _write_py(self, tmp_path, name, content):
        f = tmp_path / name
        f.write_text(content, encoding="utf-8")
        return f

    def test_dry_run_returns_preview_without_modifying(self, tmp_path):
        self._write_py(tmp_path, "a.py", "class Foo:\n    pass\nfoo = Foo()\n")
        self._write_py(tmp_path, "b.py", "from a import Foo\nprint(Foo)\n")

        result = harness_tools.rename_symbol(
            root=str(tmp_path), old_name="Foo", new_name="Bar", glob="*.py", dry_run=True,
        )
        assert result["ok"] is True
        assert result["dry_run"] is True
        assert result["files_changed"] == 2
        assert result["total_replacements"] == 4

        assert "Foo" in (tmp_path / "a.py").read_text(encoding="utf-8")
        assert "Foo" in (tmp_path / "b.py").read_text(encoding="utf-8")

    def test_apply_modifies_files(self, tmp_path):
        self._write_py(tmp_path, "x.py", "def hello():\n    return hello\n")

        result = harness_tools.rename_symbol(
            root=str(tmp_path), old_name="hello", new_name="greet", glob="*.py", dry_run=False,
        )
        assert result["ok"] is True
        assert result["dry_run"] is False
        assert result["total_replacements"] == 2

        content = (tmp_path / "x.py").read_text(encoding="utf-8")
        assert "greet" in content
        assert "hello" not in content

    def test_missing_names_error(self, tmp_path):
        result = harness_tools.rename_symbol(root=str(tmp_path), old_name="", new_name="bar")
        assert result["ok"] is False
        assert "required" in result["error"]

        result2 = harness_tools.rename_symbol(root=str(tmp_path), old_name="foo", new_name="")
        assert result2["ok"] is False

    def test_no_matches_returns_zero(self, tmp_path):
        (tmp_path / "z.py").write_text("x = 1\n", encoding="utf-8")
        result = harness_tools.rename_symbol(
            root=str(tmp_path), old_name="NonExistent", new_name="Replaced", glob="*.py",
        )
        assert result["ok"] is True
        assert result["files_changed"] == 0
        assert result["total_replacements"] == 0


class TestExtractReferences:
    def test_finds_references(self, tmp_path):
        (tmp_path / "mod.py").write_text(
            "class Widget:\n    pass\n\nw = Widget()\nprint(Widget)\n", encoding="utf-8",
        )
        result = harness_tools.extract_references(root=str(tmp_path), symbol="Widget", glob="*.py")
        assert result["ok"] is True
        assert len(result["references"]) == 3
        assert all(r["file"] == "mod.py" for r in result["references"])

    def test_no_matches_returns_empty(self, tmp_path):
        (tmp_path / "empty.py").write_text("x = 1\n", encoding="utf-8")
        result = harness_tools.extract_references(root=str(tmp_path), symbol="Missing", glob="*.py")
        assert result["ok"] is True
        assert result["references"] == []
        assert result["truncated"] is False

    def test_missing_symbol_error(self, tmp_path):
        result = harness_tools.extract_references(root=str(tmp_path), symbol="")
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_truncation_at_200(self, tmp_path):
        lines = "\n".join("use_it(Thing)" for _ in range(250))
        (tmp_path / "big.py").write_text(lines, encoding="utf-8")
        result = harness_tools.extract_references(root=str(tmp_path), symbol="Thing", glob="*.py")
        assert result["ok"] is True
        assert result["truncated"] is True
        assert len(result["references"]) == 200


class TestDiffFiles:
    def test_missing_left_file(self, tmp_path):
        (tmp_path / "b.txt").write_text("hello\n", encoding="utf-8")
        result = harness_tools.diff_files(root=str(tmp_path), left="a.txt", right="b.txt")
        assert result["ok"] is False
        assert "left file not found" in result["error"]

    def test_missing_right_file(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
        result = harness_tools.diff_files(root=str(tmp_path), left="a.txt", right="b.txt")
        assert result["ok"] is False
        assert "right file not found" in result["error"]

    def test_missing_both_paths(self, tmp_path):
        result = harness_tools.diff_files(root=str(tmp_path), left="", right="")
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_identical_files(self, tmp_path):
        (tmp_path / "a.txt").write_text("same\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("same\n", encoding="utf-8")
        result = harness_tools.diff_files(root=str(tmp_path), left="a.txt", right="b.txt")
        assert result["ok"] is True

    def test_different_files_produce_diff(self, tmp_path):
        (tmp_path / "a.txt").write_text("old line\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("new line\n", encoding="utf-8")
        result = harness_tools.diff_files(root=str(tmp_path), left="a.txt", right="b.txt")
        assert result["ok"] is True
        combined = result.get("stdout", "") + result.get("stderr", "")
        assert "old line" in combined or "new line" in combined


class TestApplyPatch:
    def test_empty_patch_error(self, tmp_path):
        result = harness_tools.apply_patch(root=str(tmp_path), patch_text="")
        assert result["ok"] is False
        assert "required" in result["error"]


class TestSecretScan:
    def test_finds_api_key(self, tmp_path):
        (tmp_path / "config.py").write_text(
            'API_KEY = "sk-abc12345678901234567890123456789abcd"\n', encoding="utf-8",
        )
        result = harness_tools.secret_scan(root=str(tmp_path))
        assert result["ok"] is True
        assert len(result["findings"]) >= 1
        types = [f["type"] for f in result["findings"]]
        assert any("key" in t.lower() for t in types)

    def test_finds_private_key_header(self, tmp_path):
        (tmp_path / "key.pem").write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nfakedata\n-----END RSA PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        result = harness_tools.secret_scan(root=str(tmp_path))
        assert result["ok"] is True
        assert any(f["type"] == "Private key" for f in result["findings"])

    def test_clean_repo_empty_findings(self, tmp_path):
        (tmp_path / "clean.py").write_text("x = 42\nprint(x)\n", encoding="utf-8")
        result = harness_tools.secret_scan(root=str(tmp_path))
        assert result["ok"] is True
        assert result["findings"] == []
        assert result["files_scanned"] >= 1

    def test_skips_binary_extensions(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n")
        (tmp_path / "secret.py").write_text(
            'password = "hunter2_is_a_real_password"\n', encoding="utf-8",
        )
        result = harness_tools.secret_scan(root=str(tmp_path))
        files_in_findings = {f["file"] for f in result["findings"]}
        assert "image.png" not in files_in_findings


class TestBuildRun:
    def test_no_build_system_error(self, tmp_path):
        result = harness_tools.build_run(root=str(tmp_path))
        assert result["ok"] is False
        assert "no recognized build system" in result["error"]

    def test_makefile_detected(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\techo ok\n", encoding="utf-8")
        result = harness_tools.build_run(root=str(tmp_path))
        assert result.get("command", [None])[0] in ("make", None)

    def test_cargo_detected(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname = \"test\"\n", encoding="utf-8")
        result = harness_tools.build_run(root=str(tmp_path))
        cmd = result.get("command", [])
        assert len(cmd) >= 1 and "cargo" in cmd[0]

    def test_package_json_detected(self, tmp_path):
        (tmp_path / "package.json").write_text('{"scripts":{"build":"echo ok"}}', encoding="utf-8")
        result = harness_tools.build_run(root=str(tmp_path))
        cmd = result.get("command", [])
        assert any("npm" in str(c) for c in cmd)

    def test_custom_command_overrides_detection(self, tmp_path):
        (tmp_path / "Makefile").write_text("all:\n\techo ok\n", encoding="utf-8")
        result = harness_tools.build_run(root=str(tmp_path), command="echo custom")
        assert result.get("command") == ["echo", "custom"]


class TestBuildClean:
    def test_no_build_system_error(self, tmp_path):
        result = harness_tools.build_clean(root=str(tmp_path))
        assert result["ok"] is False
        assert "no recognized build system" in result["error"]
