"""SPEC-3 R-M12: the architecture checker holds and stays enforceable."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_architecture_check_passes():
    result = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "scripts" / "check_architecture.py")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_root_allowlist_has_a_shrink_only_ratchet():
    import importlib.util

    path = _REPO_ROOT / "scripts" / "check_architecture.py"
    spec = importlib.util.spec_from_file_location("architecture_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert len(module.ROOT_LEGACY_MODULES) <= module.ROOT_LEGACY_MODULE_LIMIT
    assert "memory_store" not in module.ROOT_LEGACY_MODULES
    assert "eval_history" not in module.ROOT_LEGACY_MODULES
    assert module.ROOT_LEGACY_MODULES <= module.BASELINE_ROOT_LEGACY_MODULES

    removed = next(iter(module.ROOT_LEGACY_MODULES))
    module.ROOT_LEGACY_MODULES = (
        module.ROOT_LEGACY_MODULES - {removed}
    ) | {"new_accidental_legacy"}
    violations = module.check()
    assert any("new_accidental_legacy" in row for row in violations)


def test_production_callers_use_the_memory_adapter():
    offenders = []
    for path in _REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT)
        if relative == Path("memory_store.py") or relative.parts[0] == "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "memory_store" for alias in node.names
            ):
                offenders.append(str(relative))
                break
            if isinstance(node, ast.ImportFrom) and node.module == "memory_store":
                offenders.append(str(relative))
                break
    assert offenders == []


def test_checker_detects_a_violation(tmp_path):
    # Prove the checker is not vacuously green: a domain module importing
    # an adapter must fail.
    violation = (
        _REPO_ROOT / "sonder_runtime" / "domain" / "common"
        / "_test_violation.py"
    )
    violation.write_text(
        "from sonder_runtime.adapters.filesystem import atomic_json\n",
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            [sys.executable,
             str(_REPO_ROOT / "scripts" / "check_architecture.py")],
            capture_output=True,
            text=True,
            timeout=120,
        )
    finally:
        violation.unlink()
    assert result.returncode == 1
    assert "domain may not import" in result.stdout


def test_inspection_adapter_has_an_exact_read_only_legacy_dependency_set():
    path = (
        _REPO_ROOT / "sonder_runtime" / "adapters" / "legacy"
        / "inspections.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolved = {
        node.args[0].value
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_legacy_module"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        )
    }
    assert resolved == {
        "archive_tools", "content_digest", "data_query",
        "dependency_inventory", "file_ops", "log_inspect", "project_detect",
        "workspace_compare",
    }
    assert not resolved & {
        "archive_extract", "archive_create", "data_convert", "json_patch_tool",
        "sqlite_mutate", "text_patch", "workbench",
    }


def test_preference_use_case_depends_only_on_narrow_application_ports():
    use_case = (
        _REPO_ROOT / "sonder_runtime" / "application" / "preferences"
        / "use_cases.py"
    )
    tree = ast.parse(use_case.read_text(encoding="utf-8"), filename=str(use_case))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert imports == {"ports.preferences", "ports.tool_executor", "__future__"}

    adapter = (
        _REPO_ROOT / "sonder_runtime" / "adapters" / "legacy"
        / "preferences.py"
    ).read_text(encoding="utf-8")
    assert "import server" not in adapter
    assert "activity_tracker" not in adapter
    assert "archive_" not in adapter
    assert "task_" not in adapter
    assert "checklist_" not in adapter


def test_domain_modules_are_pure():
    # Importing domain modules must not touch the environment, filesystem,
    # or network. The AST checker covers imports; here we prove the
    # modules load without the heavyweight root modules present in
    # sys.modules.
    import importlib

    for name in (
        "sonder_runtime.domain.common.errors",
        "sonder_runtime.domain.common.ids",
        "sonder_runtime.domain.runtime_policy.rules",
    ):
        module = importlib.import_module(name)
        assert module is not None
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "os.environ" not in source
        assert "import sqlite3" not in source
        assert "import urllib" not in source
        assert "import subprocess" not in source


def test_task_application_service_has_no_legacy_or_sqlite_dependency():
    service = _REPO_ROOT / "sonder_runtime" / "application" / "tasks" / "use_cases.py"
    source = service.read_text(encoding="utf-8")
    assert "import memory_store" not in source
    assert "import sqlite3" not in source
    assert "import activity_tracker" not in source
    assert "import server" not in source


def test_evaluation_history_application_service_has_no_persistence_dependency():
    service = (
        _REPO_ROOT / "sonder_runtime" / "application"
        / "evaluation_history" / "use_cases.py"
    )
    source = service.read_text(encoding="utf-8")
    assert "evaluation_history_store" not in source
    assert "import eval_history" not in source
    assert "import server" not in source


def test_production_callers_use_the_evaluation_history_adapter():
    offenders = []
    for path in _REPO_ROOT.rglob("*.py"):
        relative = path.relative_to(_REPO_ROOT)
        if relative == Path("eval_history.py") or relative.parts[0] == "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                alias.name == "eval_history" for alias in node.names
            ):
                offenders.append(str(relative))
                break
            if isinstance(node, ast.ImportFrom) and node.module == "eval_history":
                offenders.append(str(relative))
                break
    assert offenders == []
