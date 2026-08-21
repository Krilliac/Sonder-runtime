from __future__ import annotations

import importlib
import hashlib
from pathlib import Path


def test_packaged_digest_streams_without_root_artifact_fetch(tmp_path):
    digest = importlib.import_module("sonder_runtime.adapters.artifact_digest")
    payload = (b"artifact bytes\x00" * 1000) + b"tail"
    target = tmp_path / "payload.bin"
    target.write_bytes(payload)

    assert digest.file_sha256(target, chunk=7) == hashlib.sha256(payload).hexdigest()


def test_artifact_fetch_uses_packaged_digest_helper():
    artifact_fetch = importlib.import_module("sonder_runtime.adapters.artifact_fetch")
    digest = importlib.import_module("sonder_runtime.adapters.artifact_digest")

    assert artifact_fetch.file_sha256 is digest.file_sha256


def test_root_artifact_fetch_module_is_retired():
    repository_root = Path(__file__).resolve().parents[1]
    assert not (repository_root / "artifact_fetch.py").exists()


def test_artifact_fetch_ownership_is_packaged():
    canonical = importlib.import_module("sonder_runtime.adapters.artifact_fetch")

    assert canonical.__name__ == "sonder_runtime.adapters.artifact_fetch"
    assert canonical.fetch_artifact.__module__ == canonical.__name__
    assert canonical.verify_artifact.__module__ == canonical.__name__
