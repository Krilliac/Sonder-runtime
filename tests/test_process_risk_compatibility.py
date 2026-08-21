"""Ownership and retirement contracts for the process-risk adapter."""
from __future__ import annotations

import ast
from pathlib import Path

import scripts.check_architecture as check_architecture


ROOT = Path(__file__).parents[1]
PACKAGED = ROOT / "sonder_runtime/adapters/process_risk.py"


def test_root_process_risk_is_retired_and_packaged_owner_exists():
    assert not (ROOT / "process_risk.py").exists()
    assert PACKAGED.is_file()
    assert Path("process_risk.py") in check_architecture.RETIRED_ROOT_MODULES


def test_process_risk_implementation_has_no_root_import_dependency():
    source = PACKAGED.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(PACKAGED))
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "process_risk" not in imports


def test_architecture_checker_retires_process_risk_root():
    assert Path("process_risk.py") in check_architecture.RETIRED_ROOT_MODULES
