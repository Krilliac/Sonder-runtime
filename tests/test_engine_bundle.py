import hashlib
import json
import os
from pathlib import Path

import pytest

import engine_bundle


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _record(root: Path, rel: str, *, executable: bool = False) -> dict:
    data = (root / rel).read_bytes()
    return {
        "path": rel,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "executable": executable,
    }


def _ollama_model(root: Path, rel: str, seed: bytes) -> tuple[str, list[str]]:
    config = b'{"model_format":"gguf"}' + seed
    layer = b"GGUF" + seed * 3
    config_hash = hashlib.sha256(config).hexdigest()
    layer_hash = hashlib.sha256(layer).hexdigest()
    config_rel = f"models/blobs/sha256-{config_hash}"
    layer_rel = f"models/blobs/sha256-{layer_hash}"
    _write(root / config_rel, config)
    _write(root / layer_rel, layer)
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "digest": f"sha256:{config_hash}",
            "size": len(config),
        },
        "layers": [
            {
                "mediaType": "application/vnd.ollama.image.model",
                "digest": f"sha256:{layer_hash}",
                "size": len(layer),
            }
        ],
    }
    _write(root / rel, json.dumps(manifest, sort_keys=True).encode("utf-8"))
    return rel, [config_rel, layer_rel]


def make_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "engine" / engine_bundle.platform_bundle_name()
    python_rel = "runtime/python/python.exe" if os.name == "nt" else "runtime/python/python3"
    ollama_rel = "runtime/ollama/ollama.exe" if os.name == "nt" else "runtime/ollama/ollama"
    _write(root / python_rel, b"portable python")
    _write(root / ollama_rel, b"portable ollama")
    base_manifest, base_blobs = _ollama_model(
        root,
        "models/manifests/registry.ollama.ai/library/qwen2.5-coder/1.5b",
        b"base",
    )
    embed_manifest, embed_blobs = _ollama_model(
        root,
        "models/manifests/registry.ollama.ai/library/nomic-embed-text/latest",
        b"embed",
    )
    rels = {
        python_rel,
        ollama_rel,
        base_manifest,
        embed_manifest,
        *base_blobs,
        *embed_blobs,
    }
    manifest = {
        "schema": 1,
        "platform": engine_bundle.normalize_platform(),
        "architecture": engine_bundle.normalize_architecture(),
        "runtime": {"python": python_rel, "ollama": ollama_rel},
        "model_store": "models",
        "base_models": [
            {
                "name": "qwen2.5-coder:1.5b",
                "manifest": base_manifest,
                "min_ram_gb": 0,
            }
        ],
        "embedding_model": {
            "name": "nomic-embed-text:latest",
            "manifest": embed_manifest,
        },
        "files": [
            _record(root, rel, executable=rel in {python_rel, ollama_rel})
            for rel in sorted(rels)
        ],
    }
    (root / engine_bundle.MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def test_loads_complete_bundle_and_selects_sealed_model(tmp_path):
    root = make_bundle(tmp_path)
    bundle = engine_bundle.load_engine_bundle(root)
    assert bundle.identity == engine_bundle.platform_bundle_name()
    assert bundle.python_executable.is_file()
    assert bundle.ollama_executable.is_file()
    assert engine_bundle.select_base_model(bundle, 64) == "qwen2.5-coder:1.5b"
    with pytest.raises(ValueError, match="not in offline bundle"):
        engine_bundle.select_base_model(bundle, 64, "qwen2.5-coder:7b")


def test_rejects_tampered_runtime(tmp_path):
    root = make_bundle(tmp_path)
    manifest = json.loads((root / engine_bundle.MANIFEST_NAME).read_text(encoding="utf-8"))
    python_rel = manifest["runtime"]["python"]
    (root / python_rel).write_bytes(b"tampered bytes")
    with pytest.raises(ValueError, match="(?:size|hash) mismatch"):
        engine_bundle.load_engine_bundle(root)


def test_rejects_incomplete_model_blob_even_without_hash_scan(tmp_path):
    root = make_bundle(tmp_path)
    manifest_path = root / engine_bundle.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = next(item for item in manifest["files"] if "/blobs/" in item["path"])
    manifest["files"].remove(missing)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="not sealed|not sealed exactly"):
        engine_bundle.load_engine_bundle(root, verify_hashes=False)


def test_rejects_blob_whose_outer_hash_disagrees_with_ollama_digest(tmp_path):
    root = make_bundle(tmp_path)
    manifest_path = root / engine_bundle.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    blob = next(item for item in manifest["files"] if "/blobs/" in item["path"])
    target = root / blob["path"]
    target.write_bytes(b"x" * target.stat().st_size)
    blob["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="digest disagrees"):
        engine_bundle.load_engine_bundle(root)


def test_rejects_wrong_platform_and_unsafe_paths(tmp_path):
    root = make_bundle(tmp_path)
    manifest_path = root / engine_bundle.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["platform"] = "linux" if engine_bundle.normalize_platform() != "linux" else "windows"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="targets"):
        engine_bundle.load_engine_bundle(root)

    root = make_bundle(tmp_path / "unsafe")
    manifest_path = root / engine_bundle.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"]["python"] = "../python"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        engine_bundle.load_engine_bundle(root)


def test_rejects_unsealed_model_store_file(tmp_path):
    root = make_bundle(tmp_path)
    _write(root / "models" / "extra.bin", b"not in manifest")
    with pytest.raises(ValueError, match="not sealed exactly"):
        engine_bundle.load_engine_bundle(root)


def test_installs_model_store_atomically_and_reuses_verified_files(tmp_path):
    root = make_bundle(tmp_path)
    bundle = engine_bundle.load_engine_bundle(root)
    home = tmp_path / "home"
    destination, copied, reused = engine_bundle.install_model_store(bundle, home)
    model_records = [item for item in bundle.files if item.relative.parts[0] == "models"]
    assert copied == len(model_records)
    assert reused == 0
    assert (destination / "SONDER-BUNDLE-RECEIPT.json").is_file()

    _, copied, reused = engine_bundle.install_model_store(bundle, home)
    assert copied == 0
    assert reused == len(model_records)
    assert not list(destination.rglob(".*.*"))


def test_runtime_environment_uses_explicit_bundle_paths(tmp_path, monkeypatch):
    root = make_bundle(tmp_path)
    bundle = engine_bundle.load_engine_bundle(root)
    monkeypatch.setenv("PATH", "system-path")
    env = engine_bundle.runtime_environment(bundle, tmp_path / "models")
    assert env["SONDER_OLLAMA_EXE"] == str(bundle.ollama_executable)
    assert env["SONDER_EMBED_MODEL"] == "nomic-embed-text:latest"
    assert env["SONDER_EMBED_REVISION"] == "ollama-manifest-sha256:" + engine_bundle.sha256_file(
        bundle.root / bundle.embedding_model.manifest
    )
    assert env["OLLAMA_MODELS"] == str(tmp_path / "models")
    assert env["OLLAMA_NO_CLOUD"] == "1"
    assert env["PATH"].startswith(str(bundle.ollama_executable.parent) + os.pathsep)


def test_default_sonder_home_honors_sonder_home(monkeypatch, tmp_path):
    expected = tmp_path / "sonder-state"
    monkeypatch.setenv("SONDER_HOME", str(expected))

    assert engine_bundle.default_sonder_home() == expected


def test_discovers_explicit_bundle_override(tmp_path, monkeypatch):
    assert engine_bundle.ENGINE_BUNDLE_ENV == "SONDER_ENGINE_BUNDLE"
    root = make_bundle(tmp_path)
    monkeypatch.setenv(engine_bundle.ENGINE_BUNDLE_ENV, str(root))
    found = engine_bundle.discover_engine_bundle(tmp_path / "unrelated")
    assert found is not None
    assert found.root == root.absolute()


def test_explicit_relative_bundle_path_is_normalized(tmp_path, monkeypatch):
    root = make_bundle(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative = root.relative_to(tmp_path)
    loaded = engine_bundle.load_engine_bundle(relative)
    assert loaded.root == root.absolute()
