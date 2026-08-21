"""Typed extension artifact admission evidence tests."""

import pytest

from sonder_runtime.domain.extensions.artifact import ExtensionArtifactReceipt


def test_receipt_consumes_verified_artifact_fetch_result():
    receipt = ExtensionArtifactReceipt.from_verification({
        "ok": True,
        "path": "C:/staging/extension.pkg",
        "sha256": "a" * 64,
        "bytes": 12,
        "final_url": "https://example.test/extension.pkg",
    })
    assert receipt.artifact_digest == "a" * 64
    assert receipt.source.startswith("https://")


@pytest.mark.parametrize("result", [
    {"ok": False, "path": "x", "sha256": "a" * 64, "bytes": 1},
    {"ok": True, "path": "x", "sha256": "bad", "bytes": 1},
    {"ok": True, "path": "x", "sha256": "a" * 64, "bytes": 0},
])
def test_receipt_rejects_unverified_or_unbounded_result(result):
    with pytest.raises(ValueError):
        ExtensionArtifactReceipt.from_verification(result)
