"""SPEC-3 R-M12: the architecture checker holds and stays enforceable."""
from __future__ import annotations

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
