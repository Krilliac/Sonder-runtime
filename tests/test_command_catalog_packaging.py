"""Architecture and compatibility tests for the canonical command catalog."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import command_catalog
from sonder_runtime.adapters import command_catalog as packaged


_ROOT = Path(__file__).resolve().parents[1]


def test_root_import_is_an_identity_redirect_to_packaged_implementation():
    assert command_catalog is packaged
    assert Path(inspect.getsourcefile(packaged)).resolve() == (
        _ROOT / "sonder_runtime" / "adapters" / "command_catalog.py"
    ).resolve()


def test_root_compatibility_module_contains_no_catalog_implementation():
    tree = ast.parse(
        (_ROOT / "command_catalog.py").read_text(encoding="utf-8"),
        filename="command_catalog.py",
    )
    assert not [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]


def test_private_and_public_catalog_surfaces_remain_available():
    for name in (
        "CATEGORIES",
        "POPULAR",
        "_CATEGORY_BY_SLASH",
        "_DANGEROUS",
        "_branch_names",
        "_source_path",
        "CatalogUnavailable",
        "catalog",
        "console_tools",
        "help_text",
        "parse_invocation",
        "reset_cache",
    ):
        assert hasattr(command_catalog, name), name


def test_packaged_catalog_keeps_lazy_server_access():
    source = (_ROOT / "sonder_runtime" / "adapters" / "command_catalog.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    top_level_imports = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.level == 0
    ] + [
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert "server" not in top_level_imports
    assert "permission_modes" not in top_level_imports


def test_source_derived_catalog_prefers_packaged_dispatch_sources():
    assert command_catalog._source_path("sonder_repl.py").endswith(
        "sonder_runtime\\interfaces\\repl\\repl.py"
    )
    assert command_catalog._source_path("sonder_serve.py").endswith(
        "sonder_runtime\\interfaces\\http\\serve.py"
    )
