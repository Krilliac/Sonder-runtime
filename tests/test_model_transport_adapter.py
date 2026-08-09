"""Compatibility and architecture tests for the model transport adapter."""
from __future__ import annotations

import importlib
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

import model_transport
from sonder_runtime.adapters.model_transport import ModelCallError as AdapterError


ROOT = Path(__file__).resolve().parents[1]


def test_root_facade_exports_exact_adapter_class_and_public_type_path():
    assert "ModelCallError" in dir(model_transport)
    assert model_transport.ModelCallError is AdapterError
    assert AdapterError.__module__ == "model_transport"
    assert issubclass(AdapterError, urllib.error.URLError)


def test_constructor_preserves_legacy_fields_and_exception_text():
    error = model_transport.ModelCallError(
        "request",
        "provider unavailable",
        transient=True,
        status=503,
        attempts=3,
        cloud=True,
    )

    assert error.kind == "request"
    assert error.detail == "provider unavailable"
    assert error.transient is True
    assert error.status == 503
    assert error.attempts == 3
    assert error.cloud is True
    assert error.reason == "provider unavailable"
    assert str(error) == "<urlopen error provider unavailable>"


def test_constructor_keeps_legacy_normalization_and_bounds():
    error = model_transport.ModelCallError("", "x" * 1000, attempts=-4)
    assert error.kind == "unknown"
    assert error.detail == "x" * 800
    assert error.attempts == 0
    assert error.status is None
    assert error.transient is False
    assert error.cloud is False

    with pytest.raises(ValueError):
        model_transport.ModelCallError("http", "bad status", status="nope")


def test_root_reload_preserves_inflight_exception_identity():
    error_type = model_transport.ModelCallError
    inflight = error_type("timeout", "slow")

    importlib.reload(model_transport)

    assert model_transport.ModelCallError is error_type
    assert isinstance(inflight, model_transport.ModelCallError)


def test_root_facade_does_not_load_adapter_until_attribute_access():
    code = "\n".join((
        "import sys",
        "import model_transport",
        "assert 'sonder_runtime.adapters.model_transport' not in sys.modules",
        "assert 'ModelCallError' in dir(model_transport)",
        "assert model_transport.ModelCallError.__name__ == 'ModelCallError'",
        "assert 'sonder_runtime.adapters.model_transport' in sys.modules",
    ))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_gateway_no_longer_imports_root_transport_module():
    gateway = (
        ROOT / "sonder_runtime" / "adapters" / "ollama" / "gateway.py"
    ).read_text(encoding="utf-8")
    adapter = (
        ROOT / "sonder_runtime" / "adapters" / "model_transport.py"
    ).read_text(encoding="utf-8")
    assert "import model_transport" not in gateway
    assert "from model_transport" not in gateway
    assert "import model_transport" not in adapter
    assert "from model_transport" not in adapter
