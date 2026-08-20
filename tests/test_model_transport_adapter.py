"""Architecture and behavior tests for the packaged model transport error."""
from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

from sonder_runtime.adapters import model_transport


ROOT = Path(__file__).resolve().parents[1]


def test_adapter_exports_stable_public_type_path():
    assert "ModelCallError" in dir(model_transport)
    assert model_transport.ModelCallError.__module__ == (
        "sonder_runtime.adapters.model_transport"
    )
    assert issubclass(model_transport.ModelCallError, urllib.error.URLError)


def test_constructor_preserves_legacy_fields_and_exception_text():
    error = model_transport.ModelCallError(
        "request", "provider unavailable", transient=True, status=503,
        attempts=3, cloud=True,
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


def test_gateway_and_adapter_do_not_import_retired_root_transport():
    gateway = (ROOT / "sonder_runtime" / "adapters" / "ollama" / "gateway.py").read_text(
        encoding="utf-8"
    )
    adapter = (ROOT / "sonder_runtime" / "adapters" / "model_transport.py").read_text(
        encoding="utf-8"
    )
    for source in (gateway, adapter):
        assert "import model_transport" not in source
        assert "from model_transport" not in source
