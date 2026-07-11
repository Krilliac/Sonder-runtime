import hashlib
import json
import os
from pathlib import Path

import pytest

import engine_bundle
from scripts import assemble_engine_bundle as assembler


def _model(store: Path, name: str, seed: bytes) -> None:
    manifest_rel = assembler._model_manifest_relative(name)
    config = b"config-" + seed
    layer = b"GGUF-" + seed * 2
    config_hash = hashlib.sha256(config).hexdigest()
    layer_hash = hashlib.sha256(layer).hexdigest()
    blobs = store / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    (blobs / f"sha256-{config_hash}").write_bytes(config)
    (blobs / f"sha256-{layer_hash}").write_bytes(layer)
    manifest = {
        "schemaVersion": 2,
        "config": {
            "digest": f"sha256:{config_hash}",
            "size": len(config),
        },
        "layers": [
            {
                "digest": f"sha256:{layer_hash}",
                "size": len(layer),
            }
        ],
    }
    target = store / manifest_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest), encoding="utf-8")


def _inputs(tmp_path: Path):
    python_runtime = tmp_path / "python-runtime"
    python_runtime.mkdir()
    python_name = "python.exe" if os.name == "nt" else "python3"
    (python_runtime / python_name).write_bytes(b"portable python")
    cache = python_runtime / "Lib" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "leaky.cpython-312.pyc").write_bytes(str(Path.home()).encode("utf-8"))
    ollama_runtime = tmp_path / "ollama-runtime"
    ollama_runtime.mkdir()
    ollama_name = "ollama.exe" if os.name == "nt" else "ollama"
    (ollama_runtime / ollama_name).write_bytes(b"portable ollama")
    model_store = tmp_path / "models"
    model_store.mkdir()
    _model(model_store, "qwen2.5-coder:1.5b", b"base")
    _model(model_store, "nomic-embed-text:latest", b"embed")
    return python_runtime, ollama_runtime, model_store


def test_assembles_and_revalidates_platform_bundle(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(assembler, "ROOT", repo)
    python_runtime, ollama_runtime, model_store = _inputs(tmp_path)
    output = repo / "dist" / "engine-bundles" / engine_bundle.platform_bundle_name()
    bundle = assembler.assemble_bundle(
        output,
        python_runtime=python_runtime,
        ollama_runtime=ollama_runtime,
        model_store=model_store,
        base_models=[("qwen2.5-coder:1.5b", 0)],
        embedding_model="nomic-embed-text:latest",
        validate_runtime=False,
    )
    assert bundle.root == output.absolute()
    assert bundle.python_executable.is_file()
    assert bundle.ollama_executable.is_file()
    assert bundle.base_models[0].name == "qwen2.5-coder:1.5b"
    assert not list(output.rglob("*.pyc"))
    assert engine_bundle.load_engine_bundle(output).manifest_sha256 == bundle.manifest_sha256


def test_missing_model_fails_before_replacing_existing_output(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(assembler, "ROOT", repo)
    python_runtime, ollama_runtime, model_store = _inputs(tmp_path)
    output = repo / "dist" / "engine-bundles" / engine_bundle.platform_bundle_name()
    output.mkdir(parents=True)
    sentinel = output / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="not installed"):
        assembler.assemble_bundle(
            output,
            python_runtime=python_runtime,
            ollama_runtime=ollama_runtime,
            model_store=model_store,
            base_models=[("missing:model", 0)],
            embedding_model="nomic-embed-text:latest",
            validate_runtime=False,
        )
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_output_is_restricted_to_exact_staging_roots(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(assembler, "ROOT", repo)
    with pytest.raises(ValueError, match="must be exactly"):
        assembler.validate_output(repo / "dist" / "engine-bundles" / "wrong-platform")


def test_model_name_maps_to_ollama_manifest_layout():
    assert assembler._model_manifest_relative("qwen2.5-coder:1.5b").as_posix() == (
        "manifests/registry.ollama.ai/library/qwen2.5-coder/1.5b"
    )
    assert assembler._model_manifest_relative("team/custom:latest").as_posix() == (
        "manifests/registry.ollama.ai/team/custom/latest"
    )
