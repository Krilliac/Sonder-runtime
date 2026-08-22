"""Ownership contracts for the packaged artifact-risk adapter."""
from __future__ import annotations

import importlib
from pathlib import Path


def test_root_artifact_risk_module_is_retired():
    repository_root = Path(__file__).resolve().parents[1]
    assert not (repository_root / "artifact_risk.py").exists()


def test_packaged_artifact_risk_owns_public_api():
    packaged = importlib.import_module("sonder_runtime.adapters.artifact_risk")
    for name in (
        "ArtifactRiskError",
        "ArtifactRiskDenied",
        "inspect_artifact",
        "format_result",
        "effective_policy",
        "enforce_execution_policy",
    ):
        assert getattr(packaged, name).__module__ == packaged.__name__
    assert packaged.policy_denies.__module__ == (
        "sonder_runtime.domain.artifact_risk_policy"
    )


def test_server_imports_packaged_artifact_risk_directly():
    source = (Path(__file__).resolve().parents[1] / "server.py").read_text(
        encoding="utf-8"
    )
    assert "import sonder_runtime.adapters.artifact_risk as artifact_risk_module" in source
    assert "import artifact_risk as artifact_risk_module" not in source


def test_packaged_artifact_risk_uses_packaged_pdf_dependency():
    artifact_risk = importlib.import_module("sonder_runtime.adapters.artifact_risk")
    packaged_pdf = importlib.import_module("sonder_runtime.adapters.pdf_risk")

    assert artifact_risk.pdf_risk is packaged_pdf
    assert artifact_risk.pdf_risk.inspect_pdf is packaged_pdf.inspect_pdf
    assert artifact_risk.pdf_risk.format_result is packaged_pdf.format_result
