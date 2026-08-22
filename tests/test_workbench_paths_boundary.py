"""Regression coverage for the workbench's canonical platform-path seam."""

import ast
from pathlib import Path

import sonder_runtime.adapters.filesystem.workbench as workbench


def test_workbench_has_no_direct_root_paths_import():
    source = Path(workbench.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "sonder_paths"
    ]
    direct_imports = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "sonder_paths"
    ]
    assert imports == []
    assert direct_imports == []


def test_run_program_uses_platform_bash_resolution(monkeypatch, tmp_path):
    monkeypatch.setattr(workbench.file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(
        workbench.runtime_paths,
        "bash_executable",
        lambda: None,
    )

    try:
        workbench.run_program("bash", cwd=".")
    except FileNotFoundError as exc:
        assert "bash/sh executable not found" in str(exc)
    else:
        raise AssertionError("run_program did not consult platform bash resolution")
