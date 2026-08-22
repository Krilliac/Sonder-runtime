"""MODEL-001 boundary tests for the remaining root-backed model seam."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "sonder_runtime"
LEGACY_MODEL = PACKAGE / "bootstrap" / "legacy_model.py"
LEGACY_ROOT = PACKAGE / "bootstrap" / "legacy_root.py"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_nonlegacy_package_callers_do_not_import_root_model_behavior():
    """MODEL-001 is blocked at one explicit bootstrap compatibility seam."""
    offenders = []
    for path in PACKAGE.rglob("*.py"):
        if path in {LEGACY_MODEL, LEGACY_ROOT}:
            continue
        imports = _imports(path)
        if (
            "server" in imports
            or "sonder_runtime.bootstrap.legacy_root" in imports
            or "sonder_runtime.bootstrap.legacy_model" in imports
        ):
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []


def test_remaining_model_root_boundary_is_explicit_and_narrow():
    """Only app composition may request the lazy legacy model factories."""
    callers = []
    for path in PACKAGE.rglob("*.py"):
        if path == LEGACY_MODEL:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.ImportFrom)
            and node.module == "legacy_model"
            and node.level == 1
            for node in ast.walk(tree)
        ) or "sonder_runtime.bootstrap.legacy_model" in _imports(path):
            callers.append(path.relative_to(ROOT).as_posix())

    assert callers == ["sonder_runtime/bootstrap/app.py"]
    assert "from .legacy_root import runtime" in LEGACY_MODEL.read_text(
        encoding="utf-8"
    )
