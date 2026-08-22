from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_composition_does_not_load_root_server_module():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        "from sonder_runtime.bootstrap.app import build_application; "
        "build_application(); "
        "print('server' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"


def test_legacy_model_module_has_no_eager_root_import():
    source = (
        Path(__file__).resolve().parents[1]
        / "sonder_runtime"
        / "bootstrap"
        / "legacy_model.py"
    ).read_text(encoding="utf-8")
    assert "from .legacy_root import" not in source.split("def configure", 1)[0]


def test_interface_compatibility_configuration_is_lazy():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        "from sonder_runtime.bootstrap.legacy_interfaces import configure_legacy_interfaces; "
        "configure_legacy_interfaces(); "
        "print('server' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"


def test_mcp_runtime_factory_is_lazy():
    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; "
        "from sonder_runtime.bootstrap.legacy_mcp import build_legacy_server_mcp_runtime; "
        "build_legacy_server_mcp_runtime(); "
        "print('server' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "False"
