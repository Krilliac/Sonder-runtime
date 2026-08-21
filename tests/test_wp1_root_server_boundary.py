"""WP1 ratchet for the one remaining legacy root-server dependency."""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "sonder_runtime"
LEGACY_ROOT = PACKAGE_ROOT / "bootstrap" / "legacy_root.py"

# Every remaining caller is explicitly excluded from this narrow migration
# slice. Keep the classification in the ratchet so a new caller cannot hide
# behind the transitional boundary.
PROHIBITED_CALLERS = {
    "sonder_runtime/bootstrap/legacy_interfaces.py": "HTTP/REPL composition",
    "sonder_runtime/bootstrap/legacy_model.py": "model bootstrap",
    "sonder_runtime/bootstrap/legacy_mcp.py": "MCP bootstrap",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_server_root_import_isolated_to_legacy_root_boundary():
    """No packaged production module may acquire ``server`` accidentally."""
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == LEGACY_ROOT:
            continue
        if "server" in _imports(path):
            offenders.append(path.relative_to(REPO_ROOT).as_posix())

    assert offenders == []
    assert "server" in _imports(LEGACY_ROOT)


def test_legacy_root_is_only_reached_by_bootstrap_composition_modules():
    """The exception remains behind explicit startup seams, not app code."""
    expected = set(PROHIBITED_CALLERS)
    callers = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if "sonder_runtime.bootstrap.legacy_root" in _imports(path) or (
            path.parent == LEGACY_ROOT.parent
            and "legacy_root" in _imports(path)
        ):
            callers.add(path.relative_to(REPO_ROOT).as_posix())

    assert callers == expected


def test_no_remaining_root_caller_is_in_the_safe_migration_scope():
    """All remaining callers are explicitly excluded seams."""
    callers = set()
    for path in PACKAGE_ROOT.rglob("*.py"):
        if path == LEGACY_ROOT:
            continue
        imports = _imports(path)
        if "sonder_runtime.bootstrap.legacy_root" in imports or (
            path.parent == LEGACY_ROOT.parent and "legacy_root" in imports
        ):
            callers.add(path.relative_to(REPO_ROOT).as_posix())

    assert callers == set(PROHIBITED_CALLERS)
    assert all(PROHIBITED_CALLERS[path] for path in callers)
