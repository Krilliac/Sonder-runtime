import hashlib
import json

import pytest

import npu_contract
import npu_manifest as m


def _write_model(directory, name="model.onnx", data=b"tiny-model-bytes"):
    target = directory / name
    target.write_bytes(data)
    return {
        "path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def _routing_manifest(directory, **overrides):
    payload = {
        "schema": 1,
        "name": "exec-route-v1",
        "operation": "routing",
        "model": _write_model(directory),
        "input": {"identity": "exec-route-features-v1", "dimension": 16},
        "labels": ["workbench", "autopilot"],
        "postprocess": "softmax",
        "providers": ["cpu-sim"],
        "limits": {"deadline_ms": 400},
    }
    payload.update(overrides)
    return payload


def _embedding_manifest(directory, **overrides):
    payload = {
        "schema": 1,
        "name": "embed-tiny-v1",
        "operation": "embedding",
        "model": _write_model(directory),
        "dimension": 8,
        "pooling": "mean",
        "normalize": True,
        "preprocess": "sim-hash-v1",
        "postprocess": "l2norm",
        "providers": ["cpu-sim"],
        "limits": {"deadline_ms": 1000, "max_batch": 4, "max_text_chars": 2000},
    }
    payload.update(overrides)
    return payload


def test_normalize_accepts_minimal_routing_manifest(tmp_path):
    manifest = m.normalize_manifest(_routing_manifest(tmp_path), tmp_path)
    assert manifest["name"] == "exec-route-v1"
    assert manifest["operation"] == "routing"
    assert manifest["providers"] == ["cpu-sim"]
    assert manifest["input"]["identity"] == "exec-route-features-v1"
    assert sorted(manifest["labels"]) == ["autopilot", "workbench"]


def test_normalize_rejects_absolute_and_traversal_model_paths(tmp_path):
    bad = _routing_manifest(tmp_path)
    bad["model"]["path"] = str(tmp_path / "model.onnx")
    with pytest.raises(ValueError):
        m.normalize_manifest(bad, tmp_path)
    bad = _routing_manifest(tmp_path)
    bad["model"]["path"] = "../outside.onnx"
    with pytest.raises(ValueError):
        m.normalize_manifest(bad, tmp_path)


def test_normalize_rejects_unknown_provider_and_labels(tmp_path):
    with pytest.raises(ValueError):
        m.normalize_manifest(
            _routing_manifest(tmp_path, providers=["gpu-magic"]), tmp_path,
        )
    with pytest.raises(ValueError):
        m.normalize_manifest(
            _routing_manifest(tmp_path, labels=["workbench", "cloud"]), tmp_path,
        )


def test_normalize_rejects_bad_hash_and_dimension(tmp_path):
    bad = _routing_manifest(tmp_path)
    bad["model"]["sha256"] = "zz" * 32
    with pytest.raises(ValueError):
        m.normalize_manifest(bad, tmp_path)
    with pytest.raises(ValueError):
        m.normalize_manifest(
            _embedding_manifest(tmp_path, dimension=0), tmp_path,
        )
    with pytest.raises(ValueError):
        m.normalize_manifest(
            _embedding_manifest(
                tmp_path, dimension=npu_contract.MAX_DIMENSION + 1,
            ),
            tmp_path,
        )


def test_normalize_clamps_limits_to_contract_caps(tmp_path):
    manifest = m.normalize_manifest(
        _embedding_manifest(
            tmp_path,
            limits={
                "deadline_ms": 10 ** 9,
                "max_batch": 10 ** 6,
                "max_text_chars": 10 ** 9,
            },
        ),
        tmp_path,
    )
    assert manifest["limits"]["deadline_ms"] == npu_contract.MAX_EMBED_DEADLINE_MS
    assert manifest["limits"]["max_batch"] == npu_contract.MAX_TEXT_ITEMS
    assert manifest["limits"]["max_text_chars"] == npu_contract.MAX_TEXT_CHARS


def test_verify_files_passes_for_matching_hash(tmp_path):
    manifest = m.normalize_manifest(_embedding_manifest(tmp_path), tmp_path)
    assert m.verify_files(manifest) == ""


def test_verify_files_reports_hash_drift(tmp_path):
    manifest = m.normalize_manifest(_embedding_manifest(tmp_path), tmp_path)
    (tmp_path / "model.onnx").write_bytes(b"tampered-model-bytes!")
    error = m.verify_files(manifest)
    assert "drift" in error or "mismatch" in error


def test_verify_files_reports_missing_file(tmp_path):
    manifest = m.normalize_manifest(_embedding_manifest(tmp_path), tmp_path)
    (tmp_path / "model.onnx").unlink()
    assert "missing" in m.verify_files(manifest)


def test_load_manifests_returns_valid_and_flags_invalid(tmp_path):
    (tmp_path / "good.json").write_text(
        json.dumps(_embedding_manifest(tmp_path)), encoding="utf-8",
    )
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    rows = m.load_manifests(tmp_path)
    by_error = {bool(row.get("error")): row for row in rows}
    assert by_error[False]["name"] == "embed-tiny-v1"
    assert by_error[False]["manifest_hash"]
    assert by_error[True]["error"]


def test_load_manifests_empty_when_directory_missing(tmp_path):
    assert m.load_manifests(tmp_path / "nope") == []


def test_manifest_hash_changes_with_content(tmp_path):
    first = m.normalize_manifest(_embedding_manifest(tmp_path), tmp_path)
    second = m.normalize_manifest(
        _embedding_manifest(tmp_path, dimension=16), tmp_path,
    )
    assert first["manifest_hash"] != second["manifest_hash"]


def test_active_manifest_picks_single_valid_per_operation(tmp_path):
    (tmp_path / "b-embed.json").write_text(
        json.dumps(_embedding_manifest(tmp_path)), encoding="utf-8",
    )
    (tmp_path / "a-route.json").write_text(
        json.dumps(_routing_manifest(tmp_path)), encoding="utf-8",
    )
    rows = m.load_manifests(tmp_path)
    assert m.active_manifest("embedding", rows)["name"] == "embed-tiny-v1"
    assert m.active_manifest("routing", rows)["name"] == "exec-route-v1"
    assert m.active_manifest("embedding", []) is None


def test_space_declaration_is_normalized_for_embeddings(tmp_path):
    manifest = m.normalize_manifest(
        _embedding_manifest(
            tmp_path,
            space={
                "model": "NOMIC-EMBED-TEXT",
                "revision": "ollama-manifest-sha256:" + "a" * 64,
            },
        ),
        tmp_path,
    )
    assert manifest["space"]["model"] == "nomic-embed-text:latest"
    assert manifest["space"]["revision"].startswith("ollama-manifest-sha256:")
