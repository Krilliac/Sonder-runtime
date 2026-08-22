from pathlib import Path

from sonder_runtime.adapters import storage
from sonder_runtime.platform import model_paths


def test_storage_keeps_model_root_compatibility_export():
    assert storage.model_roots is model_paths.model_roots


def test_model_roots_resolves_configured_path_without_creating_it(tmp_path):
    configured = tmp_path / "models"
    roots = model_paths.model_roots({"OLLAMA_MODELS": str(configured)})

    assert roots == (configured.resolve(),)
    assert not configured.exists()


def test_model_roots_deduplicates_case_insensitive_paths(monkeypatch, tmp_path):
    first = tmp_path / "models"
    second = Path(str(first).upper())
    monkeypatch.setattr(model_paths.os, "name", "nt")

    assert model_paths._unique_paths((first, second)) == (first.resolve(),)
