"""Host-side accelerator service: policy gating, host validation of scores,
provenance-gated embedding acceleration, and redacted status."""
import json

import pytest

import activity_tracker
import npu_broker
import npu_service
from tests.npu_helpers import embedding_manifest, routing_manifest


class FakeBroker:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.warmed = []

    def call(self, manifest, payload, deadline_ms=None):
        self.calls.append({"manifest": manifest, "payload": payload})
        if self.error is not None:
            raise self.error
        return self.response

    def ensure_warm(self, manifests):
        self.warmed.append(list(manifests))
        return True

    def status(self):
        return {
            "worker": {"state": "ready", "spawns": 1, "idle_unloads": 0,
                       "rss_evictions": 0, "rss_mb": 100, "pid": 42,
                       "uptime_s": 5, "idle_s": 1},
            "circuit": {"state": "closed", "opens": 0,
                        "cooldown_remaining_s": 0, "consecutive_failures": 0,
                        "deaths_without_success": 0},
            "hello": {"ort_version": "", "python": "3.12.0", "platform": "win32",
                      "ort_error": "", "pid": 42},
            "providers": [
                {"id": "cpu-sim", "detected": True, "runtime_ready": True,
                 "ep": "CPUSimulator", "reason": "stdlib deterministic simulator",
                 "label": "sim"},
                {"id": "vitisai", "detected": False, "runtime_ready": False,
                 "ep": "VitisAIExecutionProvider", "reason": "onnxruntime not installed",
                 "label": "AMD"},
            ],
            "models": [],
            "latency_ms": {"count": 1, "last": 3, "p50": 3, "p95": 3},
            "fallbacks": {},
            "last_error": "",
            "protocol": 1,
        }


@pytest.fixture
def npu_env(monkeypatch, tmp_path):
    manifest_dir = tmp_path / "npu-manifests"
    manifest_dir.mkdir()
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SONDER_NPU_MANIFEST_DIR", str(manifest_dir))
    policy_path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_path))
    npu_service.reset_for_tests()
    return manifest_dir


def _set_mode(mode, **caps):
    import runtime_policy

    runtime_policy.update(npu={"mode": mode, **caps})


def _install_routing_manifest(manifest_dir):
    manifest = routing_manifest(manifest_dir)
    (manifest_dir / "exec-route-v1.json").write_text(
        json.dumps({
            "schema": 1,
            "name": "exec-route-v1",
            "operation": "routing",
            "model": {
                "path": "route.onnx",
                "sha256": manifest["model"]["sha256"],
                "bytes": manifest["model"]["bytes"],
            },
            "input": {"identity": "exec-route-features-v1", "dimension": 16},
            "labels": ["workbench", "autopilot"],
            "postprocess": "softmax",
            "providers": ["cpu-sim"],
        }),
        encoding="utf-8",
    )
    return manifest


def _install_embedding_manifest(manifest_dir, space=None, dimension=8):
    manifest = embedding_manifest(manifest_dir, dimension=dimension)
    payload = {
        "schema": 1,
        "name": "embed-tiny-v1",
        "operation": "embedding",
        "model": {
            "path": "embed.onnx",
            "sha256": manifest["model"]["sha256"],
            "bytes": manifest["model"]["bytes"],
        },
        "dimension": dimension,
        "pooling": "mean",
        "normalize": True,
        "preprocess": "sim-hash-v1",
        "postprocess": "l2norm",
        "providers": ["cpu-sim"],
        "limits": {"max_text_chars": 50},
    }
    if space is not None:
        payload["space"] = space
    (manifest_dir / "embed-tiny-v1.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    return payload


def test_route_features_are_versioned_bounded_and_deterministic():
    features = npu_service.route_features(
        "Inspect the repo, then fix the API, and validate all tests."
    )
    assert npu_service.FEATURES_ID == "exec-route-features-v1"
    assert len(features) == npu_service.FEATURES_DIM == 16
    assert all(0.0 <= value <= 1.0 for value in features)
    assert features == npu_service.route_features(
        "Inspect the repo, then fix the API, and validate all tests."
    )
    assert features != npu_service.route_features("hi")


def test_route_decide_is_none_when_policy_off(npu_env, monkeypatch):
    _install_routing_manifest(npu_env)

    def _boom():
        raise AssertionError("broker must not be touched while npu is off")

    monkeypatch.setattr(npu_service.npu_broker, "get_broker", _boom)
    assert npu_service.route_decide("inspect, fix, and validate") is None


def test_route_decide_none_without_manifest(npu_env, monkeypatch):
    _set_mode("prefer")
    monkeypatch.setattr(
        npu_service.npu_broker, "get_broker",
        lambda: FakeBroker(response={"scores": {}}),
    )
    assert npu_service.route_decide("inspect, fix, and validate") is None


def test_route_decide_prefer_uses_validated_scores(npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    _set_mode("prefer")
    fake = FakeBroker(response={
        "scores": {"workbench": 0.13, "autopilot": 0.87},
        "reason_code": "score_margin",
        "provider": "cpu-sim",
        "ep": "CPUSimulator",
        "simulated": True,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    decision = npu_service.route_decide("inspect, fix, and validate everything")
    assert decision["mode"] == "autopilot"
    assert decision["tier"] == "code"
    assert decision["confidence"] == pytest.approx(0.87)
    assert decision["source"] == "npu accelerator"
    assert "cpu-sim" in decision["reason"]
    assert fake.calls[0]["payload"]["kind"] == "routing"
    assert len(fake.calls[0]["payload"]["features"]) == 16


def test_route_decide_falls_back_on_broker_unavailable(npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    _set_mode("prefer")
    fake = FakeBroker(error=npu_broker.NpuUnavailable("timeout"))
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.route_decide("inspect, fix, and validate") is None


def test_route_decide_rejects_invalid_scores(npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    _set_mode("prefer")
    for bad in (
        {"scores": {"workbench": 0.5, "cloud": 0.5}, "reason_code": "score_margin"},
        {"scores": {"workbench": 2.0, "autopilot": 0.1}, "reason_code": "score_margin"},
        {"scores": {"workbench": float("nan"), "autopilot": 0.1},
         "reason_code": "score_margin"},
        {"scores": {"workbench": float("inf"), "autopilot": 0.1},
         "reason_code": "score_margin"},
        {"scores": {"workbench": 0.4, "autopilot": 0.6}, "reason_code": "trust me"},
        {"nonsense": True},
    ):
        fake = FakeBroker(response=bad)
        monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
        assert npu_service.route_decide("inspect, fix, and validate") is None


def test_route_decide_requires_confident_winner(npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    _set_mode("prefer")
    fake = FakeBroker(response={
        "scores": {"workbench": 0.52, "autopilot": 0.48},
        "reason_code": "low_confidence",
        "provider": "cpu-sim",
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.route_decide("inspect, fix, and validate") is None


@pytest.mark.parametrize(
    "scores,reason_code",
    [
        ({"workbench": 1.0, "autopilot": 1.0}, "low_confidence"),
        ({"workbench": 0.9, "autopilot": 0.1}, "low_confidence"),
        ({"workbench": 0.55, "autopilot": 0.45}, "score_margin"),
    ],
)
def test_route_decide_requires_positive_consistent_margin(
    npu_env, monkeypatch, scores, reason_code,
):
    _install_routing_manifest(npu_env)
    _set_mode("prefer")
    fake = FakeBroker(response={
        "scores": scores,
        "reason_code": reason_code,
        "provider": "cpu-sim",
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.route_decide("inspect, fix, and validate") is None


def test_route_shadow_records_agreement_without_changing_anything(
    npu_env, monkeypatch,
):
    _install_routing_manifest(npu_env)
    _set_mode("shadow")
    fake = FakeBroker(response={
        "scores": {"workbench": 0.9, "autopilot": 0.1},
        "reason_code": "score_margin",
        "provider": "cpu-sim",
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    events = []
    monkeypatch.setattr(
        activity_tracker, "record_event",
        lambda kind, **fields: events.append((kind, fields)),
    )
    result = npu_service.route_shadow(
        "inspect, fix, and validate", {"mode": "workbench", "tier": "code"},
    )
    assert result is None
    assert fake.calls
    kinds = [kind for kind, _fields in events]
    assert "npu_route_shadow" in kinds
    fields = dict(events[kinds.index("npu_route_shadow")][1])
    assert fields["agree"] is True
    assert "inspect" not in json.dumps(fields)


def test_route_shadow_does_nothing_in_prefer_mode(npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    _set_mode("prefer")
    fake = FakeBroker(response={})
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    npu_service.route_shadow("work", {"mode": "workbench"})
    assert fake.calls == []


def test_embed_for_space_requires_exact_space_match(npu_env, monkeypatch):
    _set_mode("prefer")
    _install_embedding_manifest(npu_env, space={
        "model": "nomic-embed-text:latest",
        "revision": "ollama-manifest-sha256:" + "a" * 64,
    })
    fake = FakeBroker(response={
        "vectors": [[0.5, 0.5, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1]],
        "provider": "cpu-sim", "ep": "CPUSimulator", "simulated": True,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)

    match = npu_service.embed_for_space(
        "hello", "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "a" * 64,
    )
    assert match is not None
    assert match["vector"][0] == pytest.approx(0.5)
    assert match["provider"] == "cpu-sim"
    assert match["simulated"] is True

    mismatch = npu_service.embed_for_space(
        "hello", "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "b" * 64,
    )
    assert mismatch is None
    other_model = npu_service.embed_for_space(
        "hello", "other-embedder:latest",
        "ollama-manifest-sha256:" + "a" * 64,
    )
    assert other_model is None
    # Only the exact-match call reached the accelerator.
    assert len(fake.calls) == 1


def test_embed_for_space_none_without_space_declaration(npu_env, monkeypatch):
    _set_mode("prefer")
    _install_embedding_manifest(npu_env, space=None)
    fake = FakeBroker(response={"vectors": [[1.0] * 8]})
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.embed_for_space("hello", "nomic-embed-text:latest", "rev") is None
    assert fake.calls == []


def test_embed_for_space_truncates_text_to_manifest_limit(npu_env, monkeypatch):
    _set_mode("prefer")
    _install_embedding_manifest(npu_env, space={
        "model": "nomic-embed-text:latest",
        "revision": "ollama-manifest-sha256:" + "a" * 64,
    })
    fake = FakeBroker(response={
        "vectors": [[0.1] * 8], "provider": "cpu-sim", "ep": "CPUSimulator",
        "simulated": True,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    npu_service.embed_for_space(
        "X" * 500, "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "a" * 64,
    )
    assert len(fake.calls[0]["payload"]["texts"][0]) == 50


def test_embed_for_space_rejects_wrong_dimension(npu_env, monkeypatch):
    _set_mode("prefer")
    _install_embedding_manifest(npu_env, space={
        "model": "nomic-embed-text:latest",
        "revision": "ollama-manifest-sha256:" + "a" * 64,
    })
    fake = FakeBroker(response={
        "vectors": [[0.1, 0.2]], "provider": "cpu-sim", "simulated": True,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.embed_for_space(
        "hello", "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "a" * 64,
    ) is None


def test_embed_shadow_only_runs_in_shadow_mode(npu_env, monkeypatch):
    _install_embedding_manifest(npu_env)
    fake = FakeBroker(response={
        "vectors": [[0.1] * 8], "provider": "cpu-sim", "simulated": True,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    npu_service.embed_shadow("hello", "nomic-embed-text:latest", "rev")
    assert fake.calls == []
    _set_mode("shadow")
    npu_service.embed_shadow("hello", "nomic-embed-text:latest", "rev")
    assert len(fake.calls) == 1


def test_status_reports_state_axes_and_stays_redacted(npu_env, monkeypatch):
    _set_mode("shadow", routing="prefer")
    _install_routing_manifest(npu_env)
    _install_embedding_manifest(npu_env)
    fake = FakeBroker()
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    status = npu_service.status()
    assert status["enabled"] is True
    assert status["modes"] == {"routing": "prefer", "embeddings": "shadow"}
    assert status["detected"] is False  # simulator is never claimed as silicon
    assert status["runtime_ready"] is True  # cpu-sim satisfies the allowlist
    assert status["healthy"] is True
    assert status["manifests"]["routing"]["name"] == "exec-route-v1"
    assert status["manifests"]["embedding"]["name"] == "embed-tiny-v1"
    blob = json.dumps(status)
    assert "sha256" not in status["manifests"]["routing"]
    assert len(blob) < 20_000
    text = npu_service.format_status(status)
    assert "npu" in text.lower()
    assert "prefer" in text


def test_status_when_policy_off_and_never_probed(npu_env, monkeypatch):
    class ColdBroker(FakeBroker):
        def status(self):
            base = FakeBroker.status(self)
            base["worker"]["state"] = "cold"
            base["providers"] = []
            base["latency_ms"] = {"count": 0, "last": 0, "p50": 0, "p95": 0}
            return base

    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: ColdBroker())
    status = npu_service.status()
    assert status["enabled"] is False
    assert status["detected"] is None
    assert status["runtime_ready"] is None
    line = npu_service.diagnostics_line(status)
    assert "off" in line
