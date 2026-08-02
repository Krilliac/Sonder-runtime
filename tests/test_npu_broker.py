"""Broker/worker lifecycle tests against the real child process in simulator
mode: bounded JSONL stdio, lazy start, deadlines, restart-once, circuit
breaker, RSS eviction, idle unload, and honest provider fallback."""
import os
import subprocess
import sys
import time

import pytest

import npu_broker
from tests.npu_helpers import (
    embedding_manifest,
    isolate_broker_env,
    routing_manifest,
    write_model,
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


def test_call_lazily_rewarms_when_second_manifest_is_missing(
    monkeypatch, tmp_path,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    routing = routing_manifest(tmp_path)
    embedding = embedding_manifest(tmp_path)
    try:
        _warm(broker, [routing])
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(embedding, {"kind": "embedding", "texts": ["hello"]})
        assert excinfo.value.reason == "warming"
        assert broker.wait_ready(30), broker.status()
        response = broker.call(
            embedding, {"kind": "embedding", "texts": ["hello"]},
        )
        assert len(response["vectors"][0]) == embedding["dimension"]
        assert {row["name"] for row in broker.status()["models"]} == {
            routing["name"], embedding["name"],
        }
    finally:
        broker.shutdown()


def test_adding_manifest_waits_for_inflight_call_instead_of_killing_it(
    monkeypatch, tmp_path, route_features,
):
    import threading

    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_SIM_DELAY_MS="700",
    )
    routing = routing_manifest(tmp_path)
    embedding = embedding_manifest(tmp_path)
    payload = {"kind": "routing", "features": route_features}
    result = {}
    try:
        _warm(broker, [routing])
        deadline = time.monotonic() + 5
        while broker._flight.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not broker._flight.locked()

        def _run():
            try:
                result["response"] = broker.call(
                    routing, payload, deadline_ms=2000,
                )
            except Exception as exc:  # test captures any regression precisely
                result["error"] = exc

        thread = threading.Thread(target=_run)
        thread.start()
        deadline = time.monotonic() + 5
        while not broker._flight.locked() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert broker._flight.locked()
        assert broker.ensure_warm([embedding]) is True
        thread.join(timeout=10)
        assert "error" not in result
        assert set(result["response"]["scores"]) == {
            "workbench", "autopilot",
        }
        assert broker.wait_ready(30), broker.status()
        assert {row["name"] for row in broker.status()["models"]} == {
            routing["name"], embedding["name"],
        }
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
    manifest = embedding_manifest(tmp_path, providers=["vitisai"])
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


def test_post_warm_hash_drift_fails_closed_before_inference(
    monkeypatch, tmp_path, route_features,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        assert broker.call(
            manifest, {"kind": "routing", "features": route_features},
        )["ok"] is True
        # The loaded session is immutable; ordinary source metadata drift must
        # force a reload without rereading the full bundle on this hot path.
        target = tmp_path / "route.onnx"
        target.write_bytes(b"other-model")
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(
                manifest, {"kind": "routing", "features": route_features},
            )
        assert excinfo.value.reason == "manifest_unhealthy"
        status = broker.status()
        assert status["fallbacks"]["hash_drift"] >= 1
        assert status["models"][0]["ok"] is False
    finally:
        broker.shutdown()


def test_worker_environment_is_explicit_and_scrubs_cloud_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("SONDER_CLOUD_KEY", "cloud-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("QNN_AUTHORIZATION", "Bearer vendor-secret")
    monkeypatch.setenv("OPENVINO_API_KEY", "cloud-secret")
    monkeypatch.setenv("ORT_PASSWORD", "hunter2")
    monkeypatch.setenv("PYTHONPATH", "C:/private/imports")
    monkeypatch.setenv("SONDER_NPU_TEST_HOOKS", "1")
    monkeypatch.setenv("QNN_SDK_ROOT", "C:/qnn")
    environment = npu_broker._worker_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "SONDER_CLOUD_KEY" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    assert "QNN_AUTHORIZATION" not in environment
    assert "OPENVINO_API_KEY" not in environment
    assert "ORT_PASSWORD" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["SONDER_NPU_TEST_HOOKS"] == "1"
    assert environment["QNN_SDK_ROOT"] == "C:/qnn"
    assert environment["PYTHONNOUSERSITE"] == "1"


def test_worker_tries_next_allowlisted_provider_after_session_failure(
    monkeypatch, tmp_path,
):
    import npu_worker

    class FakeOrt:
        @staticmethod
        def get_available_providers():
            return ["VitisAIExecutionProvider", "CPUExecutionProvider"]

    class FakeSession:
        @staticmethod
        def get_providers():
            return ["CPUExecutionProvider"]

    attempts = []

    def fake_create(_manifest, provider_id):
        attempts.append(provider_id)
        if provider_id == "vitisai":
            return None, "failed at C:\\Users\\example\\vendor\\config.json"
        return FakeSession(), ""

    manifest = routing_manifest(tmp_path, providers=["vitisai", "cpu"])
    monkeypatch.setattr(npu_worker, "_ort", lambda: (FakeOrt(), ""))
    monkeypatch.setattr(npu_worker, "_create_real_session", fake_create)
    npu_worker._SESSIONS.clear()
    response = npu_worker._load({"id": 7, "manifest": manifest})
    assert response["ok"] is True
    assert attempts == ["vitisai", "cpu"]
    assert response["provider"] == "cpu"
    assert response["ep"] == "CPUExecutionProvider"
    assert response["ep_fallback"] is True
    assert response["npu_attested"] is False


def test_worker_rejects_failed_provider_introspection(monkeypatch, tmp_path):
    import npu_worker

    class BrokenSession:
        @staticmethod
        def get_providers():
            raise RuntimeError("cannot inspect C:\\private\\provider.dll")

    manifest = routing_manifest(tmp_path, providers=["vitisai"])
    monkeypatch.setattr(
        npu_worker, "_create_real_session", lambda *_args: (BrokenSession(), ""),
    )
    entry, error = npu_worker._entry_for_provider(
        manifest, "vitisai", "vitisai",
    )
    assert entry is None
    assert "introspection failed" in error
    assert "private" not in error


def test_worker_rejects_unallowlisted_cpu_ep_chain(monkeypatch, tmp_path):
    import npu_worker

    class AmbiguousSession:
        @staticmethod
        def get_providers():
            return ["VitisAIExecutionProvider", "CPUExecutionProvider"]

    manifest = routing_manifest(tmp_path, providers=["vitisai"])
    monkeypatch.setattr(
        npu_worker,
        "_create_real_session",
        lambda *_args: (AmbiguousSession(), ""),
    )
    entry, error = npu_worker._entry_for_provider(
        manifest, "vitisai", "vitisai",
    )
    assert entry is None
    assert "unallowlisted" in error


def test_real_session_receives_pinned_bytes_and_disables_cpu_fallback(
    monkeypatch, tmp_path,
):
    import npu_worker

    captured = {}

    class FakeOptions:
        log_severity_level = 0

        @staticmethod
        def add_session_config_entry(name, value):
            captured["config"] = (name, value)

    class FakeOrt:
        SessionOptions = FakeOptions

        @staticmethod
        def InferenceSession(model, options, providers, provider_options):
            captured.update(
                model=model,
                options=options,
                providers=providers,
                provider_options=provider_options,
            )
            return object()

    manifest = routing_manifest(tmp_path, providers=["vitisai"])
    monkeypatch.setattr(npu_worker, "_ort", lambda: (FakeOrt(), ""))
    session, error = npu_worker._create_real_session(manifest, "vitisai")
    assert session is not None
    assert error == ""
    assert captured["model"] == b"route-model"
    assert captured["providers"] == ["VitisAIExecutionProvider"]
    assert captured["config"] == ("session.disable_cpu_ep_fallback", "1")


def test_real_session_resolves_only_hash_pinned_provider_files(monkeypatch, tmp_path):
    import npu_worker

    captured = {}

    class FakeOptions:
        log_severity_level = 0

        @staticmethod
        def add_session_config_entry(*_args):
            pass

    class FakeOrt:
        SessionOptions = FakeOptions

        @staticmethod
        def InferenceSession(_model, _options, providers, provider_options):
            captured["providers"] = providers
            captured["options"] = provider_options
            return object()

    config = write_model(tmp_path, "vaip-config.json", b"{}")
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    monkeypatch.setenv("SONDER_NPU_STAGE_DIR", str(stage_dir))
    manifest = routing_manifest(
        tmp_path,
        providers=["vitisai"],
        extra_files=[config],
        provider_options={"vitisai": {"config_file": "vaip-config.json"}},
    )
    monkeypatch.setattr(npu_worker, "_ort", lambda: (FakeOrt(), ""))
    session, error = npu_worker._create_real_session(manifest, "vitisai")
    assert session is not None and error == ""
    configured = npu_worker.Path(captured["options"][0]["config_file"])
    assert not os.path.samefile(configured, tmp_path / "vaip-config.json")
    assert configured.read_bytes() == (tmp_path / "vaip-config.json").read_bytes()


def test_timeout_kills_worker_and_reports_timeout(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path, SONDER_NPU_SIM_DELAY_MS="3000",
    )
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        staging_dir = broker._worker.staging_dir
        assert staging_dir.is_dir()
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
        assert not staging_dir.exists()
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


def test_rss_cap_rejects_worker_at_ready_before_model_load(monkeypatch, tmp_path):
    broker = _fresh_broker(
        monkeypatch, tmp_path,
        SONDER_NPU_FAKE_RSS_MB="4096",
        SONDER_NPU_MAX_RSS_MB="1024",
    )
    manifest = routing_manifest(tmp_path)
    try:
        assert broker.ensure_warm([manifest]) is True
        assert broker.wait_ready(10) is False
        status = broker.status()
        assert status["worker"]["state"] == "cold"
        assert status["worker"]["rss_evictions"] == 1
        assert status["worker"]["rss_mb"] == 4096
        assert status["fallbacks"]["rss_limit"] >= 1
    finally:
        broker.shutdown()


def test_provider_runtime_readiness_clears_on_stop_and_worker_death(
    monkeypatch, tmp_path,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        broker.stop("test")
        stopped = broker.status()
        assert stopped["worker"]["state"] == "cold"
        assert not any(
            row["runtime_ready"] for row in stopped["providers"]
        )

        _warm(broker, [manifest])
        worker = broker._worker
        assert worker is not None
        broker._on_worker_dead(worker, "crash", "test death")
        dead = broker.status()
        assert dead["worker"]["state"] == "cold"
        assert not any(row["runtime_ready"] for row in dead["providers"])
    finally:
        broker.shutdown()


def test_hot_path_uses_fingerprint_without_rehashing_loaded_bundle(
        monkeypatch, tmp_path, route_features):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        monkeypatch.setattr(
            npu_broker.npu_manifest,
            "verify_files",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("inference hot path must not rehash the bundle")
            ),
        )
        monkeypatch.setattr(
            npu_broker.npu_contract,
            "sha256_file_bounded",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("inference hot path must not hash pinned files")
            ),
        )
        response = broker.call(
            manifest, {"kind": "routing", "features": route_features},
        )
        assert response["ok"] is True
        (tmp_path / "route.onnx").write_bytes(b"other-model")
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(
                manifest, {"kind": "routing", "features": route_features},
            )
        assert excinfo.value.reason == "manifest_unhealthy"
    finally:
        broker.shutdown()


def test_intentional_stop_during_warmup_does_not_open_circuit(
        monkeypatch, tmp_path):
    import threading

    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    original_wait = broker._wait_event
    entered = threading.Event()
    release = threading.Event()

    def paused_ready(worker, event, timeout_s):
        result = original_wait(worker, event, timeout_s)
        if event == "ready":
            entered.set()
            release.wait(5)
        return result

    monkeypatch.setattr(broker, "_wait_event", paused_ready)
    try:
        for _ in range(2):
            entered.clear()
            release.clear()
            assert broker.ensure_warm([manifest]) is True
            assert entered.wait(10)
            warm_thread = broker._warm_thread
            broker.stop("test")
            release.set()
            warm_thread.join(timeout=10)
            assert not warm_thread.is_alive()
        state = broker.status()
        assert state["circuit"]["state"] == "closed"
        assert state["circuit"]["deaths_without_success"] == 0
        assert state["circuit"]["consecutive_failures"] == 0
    finally:
        release.set()
        broker.shutdown()


def test_cpu_sim_is_not_registered_outside_explicit_test_hooks(monkeypatch):
    import npu_providers
    import npu_worker

    monkeypatch.delenv("SONDER_NPU_TEST_HOOKS", raising=False)
    rows = npu_providers.detect_providers(None, "missing", test_hooks=False)
    simulator = next(row for row in rows if row["id"] == "cpu-sim")
    assert simulator["registered"] is False
    assert simulator["runtime_ready"] is False
    entry, error = npu_worker._entry_for_provider(
        {"providers": ["cpu-sim"]}, "cpu-sim", "cpu-sim",
    )
    assert entry is None
    assert "test hooks" in error


def test_registered_execution_provider_is_not_reported_ready_or_detected():
    import npu_providers

    class RegisteredOnly:
        @staticmethod
        def get_available_providers():
            return ["OpenVINOExecutionProvider"]

    row = next(
        item for item in npu_providers.detect_providers(RegisteredOnly())
        if item["id"] == "openvino"
    )
    assert row["registered"] is True
    assert row["runtime_ready"] is False
    assert row["detected"] is False


@pytest.mark.parametrize("reserved", ["id", "op", "manifest_hash"])
def test_call_rejects_broker_owned_payload_keys_without_touching_worker(
    monkeypatch, tmp_path, reserved,
):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    payload = {"kind": "routing", "features": [0.5] * 16, reserved: "owned"}
    try:
        _warm(broker, [manifest])
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(manifest, payload)
        assert excinfo.value.reason == "invalid"
        assert broker.status()["worker"]["state"] == "ready"
        assert broker.call(
            manifest, {"kind": "routing", "features": [0.5] * 16},
        )["ok"] is True
    finally:
        broker.shutdown()


def test_rss_cap_is_enforced_on_worker_error_response(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path, SONDER_NPU_FAKE_RSS_MB="100")
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        monkeypatch.setenv("SONDER_NPU_MAX_RSS_MB", "64")
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(manifest, {"kind": "wrong", "features": [0.5] * 16})
        assert excinfo.value.reason == "rss_limit"
        assert broker.status()["worker"]["state"] == "cold"
    finally:
        broker.shutdown()


def test_nonfinite_worker_rss_is_malformed_and_destroys_worker(
        monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)

    class FakeWorker:
        destroyed = False

        def destroy(self):
            self.destroyed = True

    worker = FakeWorker()
    with pytest.raises(npu_broker._ExchangeError) as excinfo:
        broker._check_response_rss(worker, {"rss_mb": float("inf")}, "load")
    assert excinfo.value.reason == "malformed"
    assert worker.destroyed is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows RSS API regression")
def test_windows_worker_reports_nonzero_rss():
    import npu_worker

    assert npu_worker._rss_mb() > 0


def test_worker_must_echo_the_host_owned_manifest_hash(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        original = broker._exchange

        def mismatched(worker, payload, timeout_s):
            response = original(worker, payload, timeout_s)
            if payload.get("op") == "run":
                response = {**response, "manifest_hash": "0" * 64}
            return response

        monkeypatch.setattr(broker, "_exchange", mismatched)
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(
                manifest, {"kind": "routing", "features": [0.5] * 16},
            )
        assert excinfo.value.reason == "malformed"
        assert broker.status()["worker"]["state"] == "cold"
    finally:
        broker.shutdown()


def test_malformed_load_provenance_never_marks_model_or_npu_ready(
        monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    original = broker._exchange

    def malformed(worker, payload, timeout_s):
        response = original(worker, payload, timeout_s)
        if payload.get("op") == "load" and response.get("ok") is True:
            response = {**response, "npu_attested": "true"}
        return response

    monkeypatch.setattr(broker, "_exchange", malformed)
    try:
        assert broker.ensure_warm([manifest]) is True
        assert broker.wait_ready(10) is False
        state = broker.status()
        assert state["worker"]["state"] == "cold"
        assert not any(row.get("ok") for row in state["models"])
        assert not any(
            row.get("runtime_ready") or row.get("detected")
            for row in state.get("providers") or []
        )
    finally:
        broker.shutdown()


def test_pinned_read_stats_size_before_allocating(monkeypatch, tmp_path):
    import npu_worker

    manifest = routing_manifest(tmp_path)
    manifest["model"] = {**manifest["model"], "bytes": 1}
    monkeypatch.setattr(
        npu_worker.Path,
        "read_bytes",
        lambda _self: (_ for _ in ()).throw(AssertionError("must not allocate")),
    )
    with pytest.raises(ValueError, match="size mismatch"):
        npu_worker._read_pinned_bytes(manifest, manifest["model"], "model")


def test_pinned_read_fstats_open_handle_before_read(monkeypatch, tmp_path):
    import builtins
    import npu_worker

    manifest = routing_manifest(tmp_path)
    original_open = npu_worker.Path.open

    class ReadGuard:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, *_args, **_kwargs):
            raise AssertionError("size drift must fail before any read")

    def drift_then_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        with builtins.open(path, "ab") as writer:
            writer.write(b"x")
        return ReadGuard(stream)

    monkeypatch.setattr(npu_worker.Path, "open", drift_then_open)
    with pytest.raises(ValueError, match="size mismatch"):
        npu_worker._read_pinned_bytes(manifest, manifest["model"], "model")


def test_vendor_path_uses_private_read_only_pinned_snapshot(
        monkeypatch, tmp_path):
    import npu_contract
    import npu_worker

    manifest = routing_manifest(tmp_path)
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    monkeypatch.setenv("SONDER_NPU_STAGE_DIR", str(stage_dir))
    asset = tmp_path / "vendor" / "config.json"
    asset.parent.mkdir()
    asset.write_bytes(b'{"target":"npu"}')
    entry = {
        "path": "vendor/config.json",
        "bytes": asset.stat().st_size,
        "sha256": npu_contract.sha256_file(asset),
    }
    manifest["extra_files"] = [entry]
    manifest["manifest_hash"] = "1" * 64
    staged = npu_worker.Path(
        npu_worker._pinned_runtime_path(manifest, entry, "provider config")
    )
    assert staged != asset.resolve()
    assert staged.read_bytes() == asset.read_bytes()
    asset.write_bytes(b'{"target":"gpu"}')
    assert staged.read_bytes() == b'{"target":"npu"}'


def test_broker_rejects_combined_worker_bundle_over_limit(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    try:
        routing = routing_manifest(tmp_path)
        embedding = embedding_manifest(tmp_path)
        routing["model"] = {
            **routing["model"], "bytes": npu_broker.npu_contract.MAX_MODEL_BYTES,
        }
        embedding["model"] = {
            **embedding["model"],
            "bytes": npu_broker.npu_contract.MAX_MODEL_BYTES,
        }
        assert broker.ensure_warm([routing, embedding]) is False
        assert broker.status()["fallbacks"]["bundle_limit"] == 1
    finally:
        broker.shutdown()


def test_worker_response_queue_is_bounded_and_poisoned_on_flood():
    import queue

    worker = object.__new__(npu_broker._Worker)
    worker.queue = queue.Queue(maxsize=2)
    assert worker._enqueue({"id": 1}) is True
    assert worker._enqueue({"id": 2}) is True
    assert worker._enqueue({"id": 3}) is False
    assert worker.queue.qsize() == 1
    assert "overflow" in worker.queue.get_nowait()["_protocol_error"]


def test_staging_cleanup_rejects_replaced_directory_identity():
    import tempfile

    stage = npu_broker.Path(tempfile.mkdtemp(prefix=npu_broker._STAGING_PREFIX))
    identity = npu_broker._staging_identity(stage)
    moved = stage.with_name(stage.name + "-moved")
    stage.rename(moved)
    stage.mkdir()
    marker = stage / "marker.txt"
    marker.write_text("do not traverse", encoding="utf-8")
    try:
        npu_broker._cleanup_staging_dir(stage, identity)
        assert marker.read_text(encoding="utf-8") == "do not traverse"
    finally:
        replacement_identity = npu_broker._staging_identity(stage)
        npu_broker._cleanup_staging_dir(stage, replacement_identity)
        npu_broker._cleanup_staging_dir(moved, identity)


def test_staging_cleanup_never_chmods_through_internal_link(
        monkeypatch, tmp_path):
    import tempfile

    stage = npu_broker.Path(tempfile.mkdtemp(prefix=npu_broker._STAGING_PREFIX))
    identity = npu_broker._staging_identity(stage)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("outside", encoding="utf-8")
    try:
        try:
            os.symlink(outside, stage / "link", target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
        real_chmod = npu_broker.os.chmod
        resolved_chmods = []

        def observed_chmod(path, mode):
            resolved_chmods.append(npu_broker.Path(path).resolve())
            return real_chmod(path, mode)

        monkeypatch.setattr(npu_broker.os, "chmod", observed_chmod)
        npu_broker._cleanup_staging_dir(stage, identity)
        assert marker.resolve() not in resolved_chmods
        assert marker.read_text(encoding="utf-8") == "outside"
    finally:
        if stage.exists() and not stage.is_symlink():
            try:
                current = npu_broker._staging_identity(stage)
            except OSError:
                current = None
            npu_broker._cleanup_staging_dir(stage, current)


def test_staging_cleanup_rejects_root_symlink_substitution(
        monkeypatch, tmp_path):
    import tempfile

    stage = npu_broker.Path(tempfile.mkdtemp(prefix=npu_broker._STAGING_PREFIX))
    identity = npu_broker._staging_identity(stage)
    moved = stage.with_name(stage.name + "-original")
    outside = tmp_path / "outside-root"
    outside.mkdir()
    marker = outside / "marker.txt"
    marker.write_text("outside", encoding="utf-8")
    stage.rename(moved)
    try:
        try:
            os.symlink(outside, stage, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
        real_chmod = npu_broker.os.chmod
        resolved_chmods = []

        def observed_chmod(path, mode):
            resolved_chmods.append(npu_broker.Path(path).resolve())
            return real_chmod(path, mode)

        monkeypatch.setattr(npu_broker.os, "chmod", observed_chmod)
        npu_broker._cleanup_staging_dir(stage, identity)
        assert marker.read_text(encoding="utf-8") == "outside"
        assert marker.resolve() not in resolved_chmods
    finally:
        if stage.is_symlink():
            try:
                stage.unlink()
            except OSError:
                os.rmdir(stage)
        npu_broker._cleanup_staging_dir(moved, identity)


def test_malformed_load_error_shape_tears_down_warmup(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    original = broker._exchange

    def malformed(worker, payload, timeout_s):
        response = original(worker, payload, timeout_s)
        if payload.get("op") == "load":
            return {**response, "ok": False, "error": "not-an-object"}
        return response

    monkeypatch.setattr(broker, "_exchange", malformed)
    try:
        assert broker.ensure_warm([manifest]) is True
        assert broker.wait_ready(10) is False
        state = broker.status()
        assert state["worker"]["state"] == "cold"
        assert state["fallbacks"]["malformed"] >= 1
    finally:
        broker.shutdown()


def test_malformed_nonlist_detect_tears_down_warmup(monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    original = broker._exchange

    def malformed(worker, payload, timeout_s):
        response = original(worker, payload, timeout_s)
        if payload.get("op") == "detect":
            response = {**response, "providers": 42}
        return response

    monkeypatch.setattr(broker, "_exchange", malformed)
    try:
        assert broker.ensure_warm([manifest]) is True
        assert broker.wait_ready(10) is False
        state = broker.status()
        assert state["worker"]["state"] == "cold"
        assert state["fallbacks"]["malformed"] >= 1
    finally:
        broker.shutdown()


def test_detect_rows_are_canonicalized_and_unknown_fields_dropped(
        monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    original = broker._exchange

    def tainted_detect(worker, payload, timeout_s):
        response = original(worker, payload, timeout_s)
        if payload.get("op") == "detect":
            response["providers"] = [
                {
                    "id": "cpu-sim", "registered": True,
                    "reason": "available", "secret": "x" * 10_000,
                    "ep": "UntrustedEP", "label": "untrusted",
                },
                {"id": "cpu-sim", "registered": True, "duplicate": True},
                {"id": "cloud", "registered": True, "secret": "token"},
            ]
        return response

    monkeypatch.setattr(broker, "_exchange", tainted_detect)
    try:
        _warm(broker, [manifest])
        providers = broker.status()["providers"]
        assert len(providers) == 1
        assert providers[0]["id"] == "cpu-sim"
        assert set(providers[0]) == {
            "id", "label", "registered", "detected", "runtime_ready", "ep",
            "reason",
        }
        assert providers[0]["ep"] == "CPUSimulator"
    finally:
        broker.shutdown()


def test_run_provenance_must_match_validated_loaded_session(
        monkeypatch, tmp_path):
    broker = _fresh_broker(monkeypatch, tmp_path)
    manifest = routing_manifest(tmp_path)
    try:
        _warm(broker, [manifest])
        original = broker._exchange

        def forged(worker, payload, timeout_s):
            response = original(worker, payload, timeout_s)
            if payload.get("op") == "run" and response.get("ok") is True:
                response = {
                    **response,
                    "provider": "openvino",
                    "ep": "OpenVINOExecutionProvider",
                    "ep_chain": ["OpenVINOExecutionProvider"],
                    "simulated": False,
                    "cpu_fallback_disabled": True,
                    "npu_attested": True,
                }
            return response

        monkeypatch.setattr(broker, "_exchange", forged)
        with pytest.raises(npu_broker.NpuUnavailable) as excinfo:
            broker.call(
                manifest, {"kind": "routing", "features": [0.5] * 16},
            )
        assert excinfo.value.reason == "malformed"
        assert broker.status()["worker"]["state"] == "cold"
    finally:
        broker.shutdown()


@pytest.mark.parametrize("setting", ["truncation", "padding"])
def test_tokenizer_rejects_vector_changing_automatic_settings(
        monkeypatch, tmp_path, setting):
    from types import SimpleNamespace
    import npu_worker

    tokenizer_file = write_model(tmp_path, "tokenizer.json", b"{}")
    manifest = embedding_manifest(
        tmp_path,
        tokenizer={"type": "hf-tokenizers", **tokenizer_file},
    )
    tokenizer = SimpleNamespace(truncation=None, padding=None)
    setattr(tokenizer, setting, {"length": 2})

    class Factory:
        @staticmethod
        def from_buffer(_payload):
            return tokenizer

    monkeypatch.setitem(
        sys.modules, "tokenizers", SimpleNamespace(Tokenizer=Factory),
    )
    loaded, error = npu_worker._load_tokenizer(manifest)
    assert loaded is None
    assert setting in error


def test_real_embedding_rejects_unknown_onnx_input_name():
    from types import SimpleNamespace
    import npu_worker

    class Tokenizer:
        @staticmethod
        def encode(_text):
            return SimpleNamespace(
                ids=[1], attention_mask=[1], type_ids=[0], overflowing=[],
            )

    class Session:
        @staticmethod
        def get_inputs():
            return [
                SimpleNamespace(name="input_ids"),
                SimpleNamespace(name="position_ids"),
            ]

    entry = {"manifest": {}, "tokenizer": Tokenizer(), "session": Session()}
    with pytest.raises(ValueError, match="unsupported embedding model input"):
        npu_worker._real_embed(entry, ["hello"])


def test_real_embedding_requires_mask_for_variable_length_batch():
    from types import SimpleNamespace
    import npu_worker

    class Tokenizer:
        @staticmethod
        def encode(text):
            ids = [1] if text == "short" else [1, 2]
            return SimpleNamespace(
                ids=ids,
                attention_mask=[1] * len(ids),
                type_ids=[0] * len(ids),
                overflowing=[],
            )

    class Session:
        @staticmethod
        def get_inputs():
            return [SimpleNamespace(name="input_ids")]

        @staticmethod
        def run(_outputs, _feeds):
            raise AssertionError("model must not run without a padding mask")

    entry = {"manifest": {}, "tokenizer": Tokenizer(), "session": Session()}
    with pytest.raises(ValueError, match="requires attention_mask"):
        npu_worker._real_embed(entry, ["short", "long"])


@pytest.mark.parametrize(
    "postprocess,outputs",
    [
        ("none", [[-10.0, 10.0]]),
        ("softmax", [[0.1, 0.2, 0.7]]),
    ],
)
def test_real_routing_rejects_out_of_contract_outputs(postprocess, outputs):
    from types import SimpleNamespace
    import npu_worker

    class Session:
        @staticmethod
        def get_inputs():
            return [SimpleNamespace(name="features")]

        @staticmethod
        def run(*_args, **_kwargs):
            return outputs

    entry = {
        "manifest": {
            "input": {"dimension": 2},
            "labels": ["workbench", "autopilot"],
            "postprocess": postprocess,
        },
        "simulated": False,
        "session": Session(),
    }
    response = npu_worker._run_routing(
        entry, {"id": 1, "features": [0.1, 0.2]},
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "bad_output"


def test_worker_destroy_contains_spawned_descendant():
    psutil = pytest.importorskip("psutil")
    code = (
        "import subprocess,sys,time;"
        "sys.stdin.readline();"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(p.pid,flush=True);time.sleep(60)"
    )
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )
    job = npu_broker._WindowsJob(proc) if sys.platform == "win32" else None
    proc.stdin.write(b"go\n")
    proc.stdin.flush()
    child_pid = int(proc.stdout.readline().decode("ascii").strip())
    worker = npu_broker._Worker(proc, 1, windows_job=job)
    worker.destroy()
    deadline = time.monotonic() + 5
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not psutil.pid_exists(child_pid)


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
        assert not any(row["runtime_ready"] for row in status["providers"])
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
