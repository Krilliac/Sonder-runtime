"""Shared builders for NPU accelerator tests: manifests and broker env."""
from __future__ import annotations

import hashlib
import json


def write_model(directory, name="model.onnx", data=b"tiny-model-bytes"):
    target = directory / name
    target.write_bytes(data)
    return {
        "path": name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def routing_payload(directory, **overrides):
    payload = {
        "schema": 1,
        "name": "exec-route-v1",
        "operation": "routing",
        "model": write_model(directory, "route.onnx", b"route-model"),
        "input": {"identity": "exec-route-features-v1", "dimension": 16},
        "labels": ["workbench", "autopilot"],
        "postprocess": "softmax",
        "providers": ["cpu-sim"],
        "limits": {"deadline_ms": 1500},
    }
    payload.update(overrides)
    return payload


def embedding_payload(directory, **overrides):
    payload = {
        "schema": 1,
        "name": "embed-tiny-v1",
        "operation": "embedding",
        "model": write_model(directory, "embed.onnx", b"embed-model"),
        "dimension": 8,
        "pooling": "mean",
        "normalize": True,
        "preprocess": "sim-hash-v1",
        "postprocess": "l2norm",
        "providers": ["cpu-sim"],
        "limits": {"deadline_ms": 2000, "max_batch": 4, "max_text_chars": 2000},
    }
    payload.update(overrides)
    return payload


def write_manifest(directory, payload):
    path = directory / ("%s.json" % payload["name"])
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def routing_manifest(directory, **overrides):
    import npu_manifest

    return npu_manifest.normalize_manifest(
        routing_payload(directory, **overrides), directory,
    )


def embedding_manifest(directory, **overrides):
    import npu_manifest

    return npu_manifest.normalize_manifest(
        embedding_payload(directory, **overrides), directory,
    )


def isolate_broker_env(monkeypatch, tmp_path, **extra):
    """Pin every broker knob so tests never touch live state or real RAM."""
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "sonder-home"))
    monkeypatch.setenv("SONDER_NPU_TEST_HOOKS", "1")
    monkeypatch.setenv("SONDER_AVAILABLE_RAM_GB", "64")
    monkeypatch.setenv("SONDER_NPU_MIN_FREE_RAM_GB", "1")
    monkeypatch.setenv("SONDER_NPU_IDLE_UNLOAD_S", "600")
    monkeypatch.setenv("SONDER_NPU_MAX_RSS_MB", "8192")
    monkeypatch.setenv("SONDER_NPU_CIRCUIT_COOLDOWN_S", "120")
    for name in (
        "SONDER_NPU_SIM_DELAY_MS",
        "SONDER_NPU_SIM_CRASH_ON_RUN",
        "SONDER_NPU_SIM_GARBAGE_ON_RUN",
        "SONDER_NPU_FAKE_RSS_MB",
        "SONDER_NPU_FORCE_NO_ORT",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, str(value))
