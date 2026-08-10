"""SPEC-5 WP11 — Legacy deletion tests.

Verifies the adapters/legacy/ directory has been removed and
production code no longer imports from it.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "sonder_runtime"
LEGACY_DIR = PACKAGE_ROOT / "adapters" / "legacy"


class TestLegacyDeletion:
    def test_adapters_legacy_does_not_exist(self):
        assert not LEGACY_DIR.exists(), (
            "sonder_runtime/adapters/legacy/ must be deleted per SPEC-5 WP11"
        )

    def test_no_production_imports_from_legacy(self):
        violations = []
        for root, _dirs, files in os.walk(PACKAGE_ROOT):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = Path(root) / name
                try:
                    source = path.read_text(encoding="utf-8")
                    tree = ast.parse(source)
                except (SyntaxError, UnicodeDecodeError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        full = node.module
                        if "adapters.legacy" in full:
                            rel = path.relative_to(REPO_ROOT)
                            violations.append(f"{rel}: imports {full}")
        assert not violations, (
            "Production code still imports from adapters.legacy:\n"
            + "\n".join(violations)
        )

    def test_no_test_imports_from_legacy(self):
        """Tests should also migrate to new adapter paths."""
        violations = []
        test_dir = REPO_ROOT / "tests"
        if not test_dir.exists():
            return
        for path in test_dir.rglob("*.py"):
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if "adapters.legacy" in node.module:
                        rel = path.relative_to(REPO_ROOT)
                        violations.append(f"{rel}: imports {node.module}")
        assert not violations, (
            "Test code still imports from adapters.legacy:\n"
            + "\n".join(violations)
        )
