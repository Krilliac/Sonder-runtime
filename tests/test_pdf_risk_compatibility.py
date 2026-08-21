"""Ownership contracts for the packaged PDF risk adapter."""
from __future__ import annotations

import importlib
from pathlib import Path


def test_root_pdf_risk_module_is_retired():
    repository_root = Path(__file__).resolve().parents[1]
    assert not (repository_root / "pdf_risk.py").exists()


def test_packaged_pdf_risk_owns_public_api():
    packaged = importlib.import_module("sonder_runtime.adapters.pdf_risk")

    assert packaged.inspect_pdf.__module__ == "sonder_runtime.adapters.pdf_risk"
    assert packaged.format_result.__module__ == "sonder_runtime.adapters.pdf_risk"
    assert packaged.PdfRiskError.__module__ == "sonder_runtime.adapters.pdf_risk"
    assert packaged.__file__.replace("\\", "/").endswith(
        "sonder_runtime/adapters/pdf_risk.py"
    )


def test_packaged_pdf_risk_preserves_security_limits_and_execution_contract():
    packaged = importlib.import_module("sonder_runtime.adapters.pdf_risk")

    assert packaged.DEFAULT_MAX_SCAN_BYTES <= packaged.MAX_SCAN_BYTES
    assert packaged.DEFAULT_MAX_DECODED_BYTES <= packaged.MAX_DECODED_BYTES
    assert packaged.DEFAULT_MAX_STREAMS <= packaged.MAX_STREAMS
    assert packaged.DEFAULT_MAX_SECONDS <= packaged.MAX_SECONDS
    assert packaged.MAX_SOURCE_BYTES >= packaged.MAX_SCAN_BYTES
    assert "without rendering or execution" in packaged.inspect_pdf.__doc__
