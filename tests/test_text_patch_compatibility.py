"""Ownership contracts for the packaged text-patch adapter."""
from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).parents[1]


def test_root_text_patch_module_is_retired():
    assert not (_REPO_ROOT / "text_patch.py").exists()


def test_server_uses_packaged_text_patch_adapter_directly():
    tree = ast.parse((_REPO_ROOT / "server.py").read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    direct_imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "sonder_runtime.adapters.filesystem.text_patch" in direct_imports
    assert "text_patch" not in direct_imports
    assert "text_patch" not in imports


def test_packaged_adapter_owns_the_public_and_safety_sensitive_api():
    source = (_REPO_ROOT / "sonder_runtime/adapters/filesystem/text_patch.py").read_text(
        encoding="utf-8"
    )
    assert "class TextPatchError" in source
    assert "def text_patch" in source
    assert "def _safe_relative" in source
    assert "def _stage" in source
