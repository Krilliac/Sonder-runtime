"""Broker/worker lifecycle tests against the real child process in simulator
mode: bounded JSONL stdio, lazy start, deadlines, restart-once, circuit
breaker, RSS eviction, idle unload, and honest provider fallback."""
import time

import pytest

import npu_broker
from tests.npu_helpers import (
    embedding_manifest,
    isolate_broker_env,
    routing_manifest,
)


def _fresh_broker(monkeypatch, tmp_path, **env):
    isolate_broker_env(monkeypatch, tmp_path, **env)
    broker = npu_broker.NpuBroker()
    return broker


def _warm(broker, manifests, timeout=30.0):
    broker.ensure_warm(manifests)
    assert broker.wait_ready(timeout), broker.status()
    return broker


@pytest.fixture
def route_features():
    return [0.5] * 16


def test_call_is_lazy_and_reports_cold_before_warmup(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(manifest, {"kind": "routing", "features": [0.5] * 16})
        assert excinfo.value.reason in ("cold", "warming")
        assert broker.status()["worker"]["spawns"] <= 1
    finally:
        broker.shutdown()


def test_warm_roundtrip_routing_scores_are_allowlisted(
    monkeypatch, tmp_path, route_features,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        response = broker.call(
            manifest, {"kind": "routing", "features": route_features},
        )
        assert set(response["scores"]) == {"workbench", "autopilot"}
        assert all(0.0 <= value <= 1.0 for value in response["scores"].values())
        assert response["reason_code"] in ("score_margin", "low_confidence")
        assert response["provider"] == "cpu-sim"
        assert response["simulated"] is True
    finally:
        broker.shutdown()


def test_embedding_roundtrip_is_deterministic_and_dimensioned(
    monkeypatch, tmp_path,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = embedding_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        first = broker.call(manifest, {"kind": "embedding", "texts": ["hello"]})
        second = broker.call(manifest, {"kind": "embedding", "texts": ["hello"]})
        other = broker.call(manifest, {"kind": "embedding", "texts": ["different"]})
        assert first["vectors"] == second["vectors"]
        assert first["vectors"] != other["vectors"]
        assert len(first["vectors"][0]) == manifest["dimension"]
        assert first["simulated"] is True
    finally:
        broker.shutdown()


def test_detect_reports_simulator_and_honest_winml(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        providers = {row["id"]: row for row in broker.status()["providers"]}
        assert providers["cpu-sim"]["runtime_ready"] is True
        assert providers["winml"]["runtime_ready"] is False
        assert "DirectML" in providers["winml"]["reason"]
    finally:
        broker.shutdown()


def test_absent_onnxruntime_reports_not_ready_and_load_fails(
    monkeypatch, tmp_path,
):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_FORCE_NO_ORT="1",
    )
    manifest = embedding_manifest(tmp_path, providers=["vitisai", "cpu"])
    try:
        _warm(broker, [manifest])
        providers = {row["id"]: row for row in broker.status()["providers"]}
        assert providers["vitisai"]["runtime_ready"] is False
        assert providers["cpu"]["runtime_ready"] is False
        assert "onnxruntime" in providers["vitisai"]["reason"]
        model = broker.status()["models"][0]
        assert model["ok"] is False
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(manifest, {"kind": "embedding", "texts": ["x"]})
        assert excinfo.value.reason == "manifest_unhealthy"
    finally:
        broker.shutdown()


def test_provider_fallback_to_simulator_is_reported(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_FORCE_NO_ORT="1",
    )
    manifest = embedding_manifest(tmp_path, providers=["vitisai", "cpu-sim"])
    try:
        _warm(broker, [manifest])
        model = broker.status()["models"][0]
        assert model["ok"] is True
        assert model["provider"] == "cpu-sim"
        assert model["ep_fallback"] is True
    finally:
        broker.shutdown()


def test_no_allowlisted_provider_refuses_load(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_FORCE_NO_ORT="1",
    )
    manifest = embedding_manifest(tmp_path, providers=["qnn"])
    try:
        _warm(broker, [manifest])
        model = broker.status()["models"][0]
        assert model["ok"] is False
        assert "provider" in model["error"]
    finally:
        broker.shutdown()


def test_hash_drift_refuses_load_without_touching_worker(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = embedding_manifest(tmp_path)
    (tmp_path / "embed.onnx").write_bytes(b"tampered!")
    try:
        _warm(broker, [manifest])
        model = broker.status()["models"][0]
        assert model["ok"] is False
        assert "drift" in model["error"] or "mismatch" in model["error"]
        assert broker.status()["fallbacks"].get("hash_drift", 0) >= 1
    finally:
        broker.shutdown()


def test_timeout_kills_worker_and_reports_timeout(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_SIM_DELAY_MS="3000",
    )
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(
                manifest,
                {"kind": "routing", "features": [0.5] * 16},
                deadline_ms=200,
            )
        assert excinfo.value.reason == "timeout"
        status = broker.status()
        assert status["worker"]["state"] != "ready"
        assert status["fallbacks"]["timeout"] == 1
    finally:
        broker.shutdown()


def test_worker_crash_restart_once_then_circuit_opens(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_SIM_CRASH_ON_RUN="1",
    )
    manifest = routing_manifest(tmp_path)
    payload = {"kind": "routing", "features": [0.5] * 16}
    try:
        _warm(broker, [manifest])
        with pytest.raises(npu_broker.NpuUnavailable) as first:
            broker.call(manifest, payload)
        assert first.value.reason == "crash"
        # Restart-once: one automatic respawn is allowed after a crash.
        _warm(broker, [manifest])
        with pytest.raises(npu_broker.NpuUnavailable) as second:
            broker.call(manifest, payload)
        assert second.value.reason == "crash"
        status = broker.status()
        assert status["circuit"]["state"] == "open"
        with pytest.raises(npu_broker.NpuUnavailable) as third:
            broker.call(manifest, payload)
        assert third.value.reason == "circuit_open"
        broker.ensure_warm([manifest])
        assert broker.status()["worker"]["state"] == "cold"
    finally:
        broker.shutdown()


def test_circuit_half_opens_after_cooldown_and_recloses(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path,
        SONDER_NPU_SIM_CRASH_ON_RUN="1",
        SONDER_NPU_CIRCUIT_COOLDOWN_S="1",
    )
    manifest = routing_manifest(tmp_path)
    payload = {"kind": "routing", "features": [0.5] * 16}
    try:
        for _ in range(2):
            _warm(broker, [manifest])
            with pytest.raises(npu_broker.NpuUnavailable):
                broker.call(manifest, payload)
        assert broker.status()["circuit"]["state"] == "open"
        monkeypatch.delenv("SONDER_NPU_SIM_CRASH_ON_RUN")
        time.sleep(1.2)
        assert broker.status()["circuit"]["state"] == "half_open"
        _warm(broker, [manifest])
        response = broker.call(manifest, payload)
        assert set(response["scores"]) == {"workbench", "autopilot"}
        assert broker.status()["circuit"]["state"] == "closed"
    finally:
        broker.shutdown()


def test_malformed_worker_output_counts_and_kills(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_SIM_GARBAGE_ON_RUN="1",
    )
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(manifest, {"kind": "routing", "features": [0.5] * 16})
        assert excinfo.value.reason == "malformed"
        assert broker.status()["fallbacks"]["malformed"] == 1
        assert broker.status()["worker"]["state"] != "ready"
    finally:
        broker.shutdown()


def test_oversized_request_is_rejected_without_killing_worker(
    monkeypatch, tmp_path,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(
                manifest,
                {"kind": "routing", "features": [0.123456] * 200_000},
            )
        assert excinfo.value.reason == "oversized"
        assert broker.status()["worker"]["state"] == "ready"
    finally:
        broker.shutdown()


def test_single_flight_reports_busy(monkeypatch, tmp_path):
    import threading

    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_SIM_DELAY_MS="700",
    )
    manifest = routing_manifest(tmp_path)
    payload = {"kind": "routing", "features": [0.5] * 16}
    try:
        _warm(broker, [manifest])
        results = {}

        def _slow():
            results["slow"] = broker.call(manifest, payload, deadline_ms=2000)

        thread = threading.Thread(target=_slow)
        thread.start()
        time.sleep(0.2)
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(manifest, payload)
        assert excinfo.value.reason == "busy"
        thread.join(timeout=10)
        assert "scores" in results.get("slow", {})
    finally:
        broker.shutdown()


def test_ram_gate_blocks_spawn(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path,
        SONDER_AVAILABLE_RAM_GB="0.5",
        SONDER_NPU_MIN_FREE_RAM_GB="2",
    )
    manifest = routing_manifest(tmp_path)
    try:
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(manifest, {"kind": "routing", "features": [0.5] * 16})
        assert excinfo.value.reason == "ram_gate"
        status = broker.status()
        assert status["worker"]["state"] == "cold"
        assert status["worker"]["spawns"] == 0
        assert status["fallbacks"]["ram_gate"] >= 1
    finally:
        broker.shutdown()


def test_rss_cap_evicts_worker_after_successful_call(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path,
        SONDER_NPU_FAKE_RSS_MB="4096",
        SONDER_NPU_MAX_RSS_MB="1024",
    )
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        response = broker.call(
            manifest, {"kind": "routing", "features": [0.5] * 16},
        )
        assert "scores" in response
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = broker.status()
            if status["worker"]["state"] == "cold":
                break
            time.sleep(0.05)
        assert status["worker"]["state"] == "cold"
        assert status["worker"]["rss_evictions"] == 1
    finally:
        broker.shutdown()


def test_idle_unload_stops_worker(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_IDLE_UNLOAD_S="1",
    )
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = broker.status()
            if status["worker"]["state"] == "cold":
                break
            time.sleep(0.1)
        assert status["worker"]["state"] == "cold"
        assert status["worker"]["idle_unloads"] == 1
    finally:
        broker.shutdown()


def test_status_is_bounded_and_never_carries_text_or_vectors(
    monkeypatch, tmp_path,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = embedding_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        broker.call(
            manifest,
            {"kind": "embedding", "texts": ["secret prompt text never logged"]},
        )
        status = broker.status()
        blob = repr(status)
        assert "secret prompt" not in blob
        assert len(blob) < 20_000
        assert status["latency_ms"]["count"] == 1
        assert isinstance(status["latency_ms"]["p95"], int)
    finally:
        broker.shutdown()
