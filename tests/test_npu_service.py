"""Host-side accelerator service: policy gating, host validation of scores,
provenance-gated embedding acceleration, and redacted status."""
import json
import threading

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
        self.model_manifest_hash = ""

    def call(self, manifest, payload, deadline_ms=None):
        self.calls.append({"manifest": manifest, "payload": payload})
        if self.error is not None:
            raise self.error
        if not isinstance(self.response, dict):
            return self.response
        response = dict(self.response)
        provider = str(response.get("provider") or "")
        ep = str(response.get("ep") or "")
        if provider and ep:
            response.setdefault("ep_chain", [ep])
            response.setdefault(
                "cpu_fallback_disabled",
                provider in {"vitisai", "openvino", "qnn"},
            )
            response.setdefault(
                "ep_fallback",
                provider != (manifest.get("providers") or [""])[0],
            )
            response.setdefault("npu_attested", False)
        return response

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
                {"id": "cpu-sim", "registered": True, "detected": False,
                 "runtime_ready": True,
                 "ep": "CPUSimulator", "reason": "stdlib deterministic simulator",
                 "label": "sim"},
                {"id": "vitisai", "detected": False, "runtime_ready": False,
                 "ep": "VitisAIExecutionProvider", "reason": "onnxruntime not installed",
                 "label": "AMD"},
            ],
            "models": [{
                "ok": True,
                "provider": "cpu-sim",
                "simulated": True,
                "manifest_hash": self.model_manifest_hash,
            }],
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
    monkeypatch.setattr(
        npu_service.system_profile,
        "detect_npu_hardware",
        lambda: ("none", "", False),
    )
    npu_service.reset_for_tests()
    return manifest_dir


def _set_mode(mode, **caps):
    import runtime_policy

    runtime_policy.update(npu={"mode": mode, **caps})


def _install_routing_manifest(manifest_dir, providers=None):
    providers = providers or ["cpu-sim"]
    manifest = routing_manifest(manifest_dir, providers=providers)
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
            "providers": providers,
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
        payload["space"] = {
            **space,
            "dimension": space.get("dimension", dimension),
        }
    (manifest_dir / "embed-tiny-v1.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    return payload


def test_route_features_are_versioned_bounded_and_deterministic():
    features = npu_service.route_features(
        "Inspect the repo, then fix the API, and validate all tests."
    )
    assert npu_service.LEGACY_FEATURES_ID == "exec-route-features-v1"
    assert len(features) == npu_service.LEGACY_FEATURES_DIM == 16
    assert all(0.0 <= value <= 1.0 for value in features)
    assert features == npu_service.route_features(
        "Inspect the repo, then fix the API, and validate all tests."
    )
    assert features != npu_service.route_features("hi")


def test_route_features_v2_extend_v1_and_stay_bounded():
    prompt = (
        "Inspect the repo, then fix the API, and validate all tests. "
        "1) refactor the config 2) document the changes."
    )
    features = npu_service.route_features_v2(prompt)
    assert npu_service.FEATURES_ID == "exec-route-features-v2"
    assert len(features) == npu_service.FEATURES_DIM == 36
    assert features[:16] == npu_service.route_features(prompt)
    assert all(0.0 <= value <= 1.0 for value in features)
    assert features == npu_service.route_features_v2(prompt)
    # semantic dims must differ between create-heavy and inspect-heavy asks
    create = npu_service.route_features_v2(
        "Build the api, write the docs, then create the ui in ./src."
    )
    inspect = npu_service.route_features_v2(
        "Audit the api, review the docs, then inspect the ui in ./src."
    )
    assert create[16:] != inspect[16:]
    assert set(npu_service.FEATURE_CONTRACTS) == {
        npu_service.LEGACY_FEATURES_ID, npu_service.FEATURES_ID,
    }


@pytest.mark.parametrize("field", [
    "simulated", "cpu_fallback_disabled", "ep_fallback",
])
@pytest.mark.parametrize("bad_value", [None, "false"])
def test_execution_provenance_requires_literal_boolean_fields(
        tmp_path, field, bad_value):
    manifest = routing_manifest(tmp_path)
    response = {
        "provider": "cpu-sim",
        "ep": "CPUSimulator",
        "ep_chain": ["CPUSimulator"],
        "simulated": True,
        "cpu_fallback_disabled": False,
        "ep_fallback": False,
        "npu_attested": False,
    }
    if bad_value is None:
        response.pop(field)
    else:
        response[field] = bad_value
    with pytest.raises(ValueError, match="boolean"):
        npu_service._execution_provenance(response, manifest)


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
    assert decision["source"] == "cpu simulator"
    assert "cpu-sim" in decision["reason"]
    assert fake.calls[0]["payload"]["kind"] == "routing"
    assert len(fake.calls[0]["payload"]["features"]) == 16


def test_route_decide_cpu_reference_is_not_labeled_npu(npu_env, monkeypatch):
    _install_routing_manifest(npu_env, providers=["vitisai", "cpu"])
    _set_mode("prefer")
    fake = FakeBroker(response={
        "scores": {"workbench": 0.1, "autopilot": 0.9},
        "reason_code": "score_margin",
        "provider": "cpu",
        "ep": "CPUExecutionProvider",
        "ep_fallback": True,
        "simulated": False,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    decision = npu_service.route_decide("inspect, fix, and validate everything")
    assert decision["source"] == "cpu reference"
    assert "npu accelerator" not in decision["reason"]


def test_route_decide_rejects_ambiguous_npu_cpu_chain(npu_env, monkeypatch):
    _install_routing_manifest(npu_env, providers=["vitisai", "cpu"])
    _set_mode("prefer")
    fake = FakeBroker(response={
        "scores": {"workbench": 0.1, "autopilot": 0.9},
        "reason_code": "score_margin",
        "provider": "vitisai",
        "ep": "VitisAIExecutionProvider",
        "ep_chain": ["VitisAIExecutionProvider", "CPUExecutionProvider"],
        "cpu_fallback_disabled": True,
        "ep_fallback": False,
        "simulated": False,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.route_decide("inspect, fix, and validate everything") is None


def test_route_decide_rejects_unsupported_vitis_npu_attestation(
        npu_env, monkeypatch):
    _install_routing_manifest(npu_env, providers=["vitisai"])
    _set_mode("prefer")
    fake = FakeBroker(response={
        "scores": {"workbench": 0.1, "autopilot": 0.9},
        "reason_code": "score_margin",
        "provider": "vitisai",
        "ep": "VitisAIExecutionProvider",
        "ep_chain": ["VitisAIExecutionProvider"],
        "cpu_fallback_disabled": True,
        # VitisAI spans multiple target classes. Until its effective target can
        # be proved, a worker claim that it ran on an NPU must be rejected.
        "npu_attested": True,
        "simulated": False,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.route_decide("inspect, fix, and validate everything") is None


def test_route_decide_falls_back_on_broker_unavailable(npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    _set_mode("prefer")
    fake = FakeBroker(error=npu_broker.NpuUnavailable("timeout"))
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.route_decide("inspect, fix, and validate") is None


def test_fallback_projection_reports_ram_gate_and_execution_handler(
    npu_env, monkeypatch,
):
    _set_mode("shadow", embeddings="prefer")
    fake = FakeBroker()
    broker_status = fake.status()
    broker_status["fallbacks"] = {"ram_gate": 1}
    broker_status["last_error"] = (
        r"available RAM below spawn gate at C:\private\models; token=secret"
    )
    monkeypatch.setattr(fake, "status", lambda: broker_status)
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)

    state = npu_service.status()
    projection = npu_service.fallback_projection(state)

    assert projection["schema_version"] == 1
    assert projection["capabilities"]["routing"] == {
        "policy_mode": "shadow",
        "role": "observer",
        "local_fallback_handler": "ollama",
    }
    assert projection["capabilities"]["embeddings"] == {
        "policy_mode": "prefer",
        "role": "executor",
        "local_fallback_handler": "ollama",
    }
    assert projection["reason_counts"] == {"ram_gate": 1}
    assert projection["last_fallback"] == {
        "capability": "unknown",
        "reason": "ram_gate",
        "operation_mode": "unknown",
        "fallback_handler": "unknown",
        "handler_state": "observed",
        "count": 1,
    }
    assert "available RAM" not in json.dumps(projection)


def test_unknown_broker_reason_is_collapsed_before_human_status(
    npu_env, monkeypatch,
):
    _set_mode("prefer")
    fake = FakeBroker()
    broker_status = fake.status()
    hostile = r"C:\private\weights\model.onnx token=secret\x1b[2J"
    broker_status["fallbacks"] = {hostile: 3}
    broker_status["last_error"] = hostile
    broker_status["hello"]["ort_error"] = "private prompt payload " + hostile
    broker_status["providers"][0]["reason"] = "private prompt payload " + hostile
    broker_status["worker"]["state"] = "private prompt payload " + hostile
    broker_status["worker"]["spawns"] = "private prompt payload " + hostile
    broker_status["circuit"]["state"] = "private prompt payload " + hostile
    broker_status["latency_ms"]["p95"] = "private prompt payload " + hostile
    monkeypatch.setattr(fake, "status", lambda: broker_status)
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)

    state = npu_service.status()
    rendered = npu_service.format_status(state)
    projection = npu_service.fallback_projection(state)

    assert projection["reason_counts"] == {"unknown": 3}
    assert projection["last_fallback"]["reason"] == "unknown"
    assert "unknown=3" in rendered
    assert hostile not in rendered
    assert "private" not in rendered and "token=secret" not in rendered
    assert "prompt payload" not in rendered
    assert "prompt payload" not in npu_service.diagnostics_line(state)


def test_activity_feed_distinguishes_execution_fallback_from_shadow(npu_env):
    activity_tracker.reset_for_tests()
    with activity_tracker.response_span("npu-observability", "safe request"):
        npu_service._record_capability_event(
            "npu_route_shadow", "routing", reason="ram_gate", ok=False,
            handler="ollama",
        )
        npu_service._record_fallback("embeddings", "ram_gate")
        npu_service.record_fallback_handler("embeddings", "ollama", True)

    feed = activity_tracker.execution_feed(include_detail=False)
    assert feed["schema_version"] == 1
    events = [row for row in feed["events"] if row["kind"].startswith("npu_")]
    shadow = next(row for row in events if row["kind"] == "npu_route_shadow")
    pending = next(row for row in events if row["kind"] == "npu_fallback")
    handled = next(
        row for row in events if row["kind"] == "npu_fallback_handled"
    )
    assert (shadow["capability"], shadow["operation_mode"]) == (
        "routing", "shadow",
    )
    assert (shadow["fallback_handler"], shadow["handler_state"]) == (
        "ollama", "observed",
    )
    assert (pending["capability"], pending["operation_mode"]) == (
        "embeddings", "execution",
    )
    assert pending["handler_state"] == "pending"
    assert (handled["fallback_handler"], handled["handler_state"]) == (
        "ollama", "handled",
    )
    assert npu_service.fallback_projection()["last_fallback"][
        "handler_state"
    ] == "handled"


def test_hostile_runtime_reason_never_enters_activity_or_status(npu_env):
    activity_tracker.reset_for_tests()
    hostile = r"C:\private\weights\model.onnx api_key=secret"
    with activity_tracker.response_span("npu-hostile", "safe request"):
        npu_service._record_fallback("embeddings", hostile)

    raw = activity_tracker.snapshot()
    feed = activity_tracker.execution_feed(raw, include_detail=True)
    projection = npu_service.fallback_projection()
    serialized = json.dumps({"raw": raw, "feed": feed, "status": projection})

    assert hostile not in serialized
    event = next(row for row in feed["events"] if row["kind"] == "npu_fallback")
    assert event["reason"] == "unknown"
    assert projection["last_fallback"]["reason"] == "unknown"


def test_public_fallback_projection_revalidates_supplied_state(npu_env):
    secret = r"C:\private\model.onnx token=secret"
    projection = npu_service.fallback_projection({
        "fallback_projection": {
            "schema_version": 999,
            "known": True,
            "capabilities": {
                "routing": {
                    "policy_mode": secret,
                    "role": secret,
                    "local_fallback_handler": secret,
                },
                "embeddings": {
                    "policy_mode": "prefer",
                    "role": "administrator",
                    "local_fallback_handler": secret,
                },
                secret: {"policy_mode": "prefer"},
            },
            "last_fallback": {
                "capability": secret, "reason": secret,
                "operation_mode": secret, "fallback_handler": secret,
                "handler_state": secret, "count": True,
                "raw_error": secret,
            },
            "reason_counts": {secret: True, "ram_gate": "999999999999"},
            "raw_error": secret,
        },
    })

    assert projection["schema_version"] == 1
    assert projection["capabilities"]["routing"]["policy_mode"] == "unknown"
    assert projection["capabilities"]["embeddings"] == {
        "policy_mode": "prefer", "role": "executor",
        "local_fallback_handler": "ollama",
    }
    assert projection["last_fallback"] == {
        "capability": "unknown", "reason": "unknown",
        "operation_mode": "unknown", "fallback_handler": "unknown",
        "handler_state": "unknown", "count": 0,
    }
    assert projection["reason_counts"] == {
        "unknown": 0, "ram_gate": 2_147_483_647,
    }
    assert secret not in json.dumps(projection)


def test_malformed_fallback_counter_container_is_unknown_not_error(npu_env):
    projection = npu_service._fallback_status(
        {"routing": "shadow", "embeddings": "prefer"},
        {"fallbacks": [r"C:\private\model token=secret"]},
    )
    assert projection["known"] is False
    assert projection["reason_counts"] == {}
    assert projection["last_fallback"]["reason"] == "unknown"


def test_concurrent_fallback_handlers_keep_their_own_reason(npu_env):
    activity_tracker.reset_for_tests()
    first_recorded = threading.Event()
    second_handled = threading.Event()

    def first():
        with activity_tracker.response_span("first-fallback", "safe"):
            npu_service._record_fallback("embeddings", "ram_gate")
            first_recorded.set()
            assert second_handled.wait(timeout=3)
            npu_service.record_fallback_handler("embeddings", "ollama", True)

    def second():
        assert first_recorded.wait(timeout=3)
        with activity_tracker.response_span("second-fallback", "safe"):
            npu_service._record_fallback("embeddings", "timeout")
            npu_service.record_fallback_handler("embeddings", "ollama", False)
        second_handled.set()

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    feed = activity_tracker.execution_feed(include_detail=False)
    handled = [
        row for row in feed["events"]
        if row["kind"] == "npu_fallback_handled"
    ]
    assert {(row["reason"], row["handler_state"]) for row in handled} == {
        ("ram_gate", "handled"), ("timeout", "failed"),
    }
    latest = npu_service.fallback_projection()["last_fallback"]
    assert (latest["reason"], latest["handler_state"]) == ("timeout", "failed")


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
        "ep": "CPUSimulator",
        "simulated": True,
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


def test_simulator_never_substitutes_for_a_production_space(npu_env, monkeypatch):
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
        expected_dimension=8,
    )
    assert match is None

    mismatch = npu_service.embed_for_space(
        "hello", "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "b" * 64,
        expected_dimension=8,
    )
    assert mismatch is None
    other_model = npu_service.embed_for_space(
        "hello", "other-embedder:latest",
        "ollama-manifest-sha256:" + "a" * 64,
        expected_dimension=8,
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


def test_embed_for_space_falls_back_instead_of_truncating_text(npu_env, monkeypatch):
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
    result = npu_service.embed_for_space(
        "X" * 500, "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "a" * 64,
        expected_dimension=8,
    )
    assert result is None
    assert fake.calls == []


def test_embed_for_space_rejects_target_unattested_vitis_result(
        npu_env, monkeypatch):
    _set_mode("prefer")
    payload = _install_embedding_manifest(npu_env, space={
        "model": "nomic-embed-text:latest",
        "revision": "ollama-manifest-sha256:" + "a" * 64,
    })
    payload["providers"] = ["vitisai"]
    (npu_env / "embed-tiny-v1.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    fake = FakeBroker(response={
        "vectors": [[0.1] * 8],
        "provider": "vitisai",
        "ep": "VitisAIExecutionProvider",
        "simulated": False,
        "npu_attested": False,
    })
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    assert npu_service.embed_for_space(
        "hello", "nomic-embed-text:latest",
        "ollama-manifest-sha256:" + "a" * 64,
        expected_dimension=8,
    ) is None
    assert len(fake.calls) == 1


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
        expected_dimension=8,
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
    fake.model_manifest_hash = npu_service._active("routing")["manifest_hash"]
    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: fake)
    status = npu_service.status()
    assert status["enabled"] is True
    assert status["modes"] == {"routing": "prefer", "embeddings": "shadow"}
    assert status["detected"] is False  # simulator is never claimed as silicon
    assert status["utility_ready"] is True  # cpu-sim can run bounded utility work
    assert status["runtime_ready"] is False  # but cannot make the NPU look ready
    assert status["healthy"] is True
    assert status["manifests"]["routing"]["name"] == "exec-route-v1"
    assert status["manifests"]["embedding"]["name"] == "embed-tiny-v1"
    blob = json.dumps(status)
    assert "sha256" not in status["manifests"]["routing"]
    assert len(blob) < 20_000
    text = npu_service.format_status(status)
    assert "npu" in text.lower()
    assert "prefer" in text


def test_status_maps_provider_detection_only_from_host_npu_vendor(
        npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    monkeypatch.setattr(
        npu_service.system_profile,
        "detect_npu_hardware",
        lambda: ("amd", "AMD Ryzen AI", True),
    )
    monkeypatch.setattr(
        npu_service.system_profile,
        "detect_hardware",
        lambda: (_ for _ in ()).throw(
            AssertionError("status must not run the full hardware probe")
        ),
    )
    monkeypatch.setattr(
        npu_service.npu_broker, "get_broker", lambda: FakeBroker(),
    )
    state = npu_service.status()
    providers = {row["id"]: row for row in state["broker"]["providers"]}
    assert state["detected"] is True
    assert providers["vitisai"]["detected"] is True
    assert providers["cpu-sim"]["detected"] is False


def test_status_drops_unknown_duplicate_and_extra_provider_fields(
        npu_env, monkeypatch):
    class TaintedBroker(FakeBroker):
        def status(self):
            state = super().status()
            state["providers"] = [
                {
                    "id": "cpu-sim", "registered": True,
                    "runtime_ready": True, "reason": "ready",
                    "secret": "x" * 10_000,
                },
                {"id": "cpu-sim", "registered": True, "duplicate": True},
                {"id": "cloud", "registered": True, "token": "secret"},
            ]
            return state

    monkeypatch.setattr(
        npu_service.npu_broker, "get_broker", lambda: TaintedBroker(),
    )
    providers = npu_service.status()["broker"]["providers"]
    assert len(providers) == 1
    assert set(providers[0]) == {
        "id", "label", "registered", "detected", "runtime_ready", "ep",
        "reason",
    }


def test_status_contains_nonfinite_manifest_error_without_crashing(
        npu_env, monkeypatch):
    payload = routing_manifest(npu_env)
    raw = {
        "schema": payload["schema"],
        "name": payload["name"],
        "operation": payload["operation"],
        "model": payload["model"],
        "input": payload["input"],
        "labels": payload["labels"],
        "postprocess": payload["postprocess"],
        "providers": payload["providers"],
        "limits": {"deadline_ms": "OVERFLOW"},
    }
    serialized = json.dumps(raw).replace('"OVERFLOW"', "1e400")
    (npu_env / "overflow.json").write_text(serialized, encoding="utf-8")
    monkeypatch.setattr(
        npu_service.npu_broker, "get_broker", lambda: FakeBroker(),
    )
    state = npu_service.status()
    assert state["manifest_errors"] == 1
    assert state["runtime_ready"] is False


def test_status_ignores_loaded_model_after_manifest_replacement_or_removal(
        npu_env, monkeypatch):
    _install_routing_manifest(npu_env)
    original = npu_service._active("routing")
    fake = FakeBroker()
    fake.model_manifest_hash = original["manifest_hash"]
    monkeypatch.setattr(
        npu_service.npu_broker, "get_broker", lambda: fake,
    )

    initial = npu_service.status()
    assert initial["utility_ready"] is True
    assert initial["broker"]["providers"][0]["runtime_ready"] is True

    path = npu_env / "exec-route-v1.json"
    replacement = json.loads(path.read_text(encoding="utf-8"))
    # Change both content and size so even coarse filesystems invalidate the
    # intentionally metadata-based manifest cache deterministically.
    replacement["name"] = "exec-route-v2-replacement"
    path.write_text(json.dumps(replacement), encoding="utf-8")
    replaced = npu_service.status()
    assert replaced["utility_ready"] is False
    assert replaced["runtime_ready"] is False
    assert replaced["broker"]["models"] == []
    assert not any(
        row["runtime_ready"] for row in replaced["broker"]["providers"]
    )

    path.unlink()
    removed = npu_service.status()
    assert removed["manifest_count"] == 0
    assert removed["utility_ready"] is False
    assert removed["runtime_ready"] is False


def test_status_when_policy_off_and_never_probed(npu_env, monkeypatch):
    class ColdBroker(FakeBroker):
        def status(self):
            base = FakeBroker.status(self)
            base["worker"]["state"] = "cold"
            base["providers"] = []
            base["models"] = []
            base["latency_ms"] = {"count": 0, "last": 0, "p50": 0, "p95": 0}
            return base

    monkeypatch.setattr(npu_service.npu_broker, "get_broker", lambda: ColdBroker())
    status = npu_service.status()
    assert status["enabled"] is False
    assert status["detected"] is False
    assert status["utility_ready"] is None
    assert status["runtime_ready"] is None
    line = npu_service.diagnostics_line(status)
    assert "off" in line


def test_status_redacts_vendor_paths_and_credentials(npu_env, monkeypatch):
    class LeakyBroker(FakeBroker):
        def status(self):
            state = super().status()
            state["hello"]["ort_error"] = (
                "failed C:\\Users\\example\\vendor.dll token=secret-value"
            )
            state["providers"][1]["reason"] = "/home/example/vendor/config.xml"
            state["last_error"] = "api_key=another-secret"
            return state

    monkeypatch.setattr(
        npu_service.npu_broker, "get_broker", lambda: LeakyBroker(),
    )
    state = npu_service.status()
    rendered = json.dumps(state) + npu_service.format_status(state)
    assert "example" not in rendered
    assert "secret-value" not in rendered
    assert "/home/example" not in rendered
    assert "another-secret" not in rendered
    assert "<redacted>" in rendered or "<path>" in rendered


def test_shadow_ledger_persists_and_advises(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SONDER_NPU_SHADOW_LEDGER", str(tmp_path / "ledger.json"),
    )
    npu_service.reset_for_tests()
    advisory = npu_service.shadow_advisory()
    assert advisory["window"] == 0
    assert advisory["prefer_supported"] is False
    assert "insufficient" in advisory["advisory"]

    for _ in range(60):
        npu_service._ledger_record("agree")
    npu_service._ledger_record("disagree")
    npu_service._ledger_record("error")

    advisory = npu_service.shadow_advisory()
    assert advisory["window"] == 61
    assert advisory["prefer_supported"] is True
    assert advisory["totals"] == {"agree": 60, "disagree": 1, "errors": 1}
    assert "prefer" in advisory["advisory"]

    # a process restart (reset) must reload the persisted evidence
    npu_service.reset_for_tests()
    advisory = npu_service.shadow_advisory()
    assert advisory["window"] == 61
    assert advisory["totals"]["agree"] == 60


def test_shadow_ledger_low_agreement_does_not_advise_prefer(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(
        "SONDER_NPU_SHADOW_LEDGER", str(tmp_path / "ledger.json"),
    )
    npu_service.reset_for_tests()
    for index in range(60):
        npu_service._ledger_record(
            "agree" if index % 2 == 0 else "disagree",
        )
    advisory = npu_service.shadow_advisory()
    assert advisory["window"] == 60
    assert advisory["prefer_supported"] is False
    assert "below" in advisory["advisory"]


def test_ram_gate_fallbacks_are_reported_as_a_cost(npu_env, monkeypatch):
    """A bare "ram_gate=357" reads as harmless bookkeeping. Each one is an
    embedding the model server handled instead, and under a one-model cap that
    evicts and reloads the generation model - measured at ~7.5s per swap. The
    status must say so and name the knob."""
    broker = FakeBroker()
    base = broker.status()
    base["fallbacks"] = {"ram_gate": 357, "warming": 2}
    monkeypatch.setattr(broker, "status", lambda: base)
    monkeypatch.setattr(npu_broker, "get_broker", lambda: broker)

    text = npu_service.format_status()
    assert "ram_gate=357" in text
    assert "357 call(s) fell back for free RAM" in text
    assert "SONDER_NPU_MIN_FREE_RAM_GB" in text


def test_a_non_dominant_ram_gate_does_not_claim_the_cost(npu_env, monkeypatch):
    """The note is only meaningful when the RAM gate is the main reason calls
    fall back; a stray one alongside a dominant other cause should not."""
    broker = FakeBroker()
    base = broker.status()
    base["fallbacks"] = {"ram_gate": 1, "invalid": 90}
    monkeypatch.setattr(broker, "status", lambda: base)
    monkeypatch.setattr(npu_broker, "get_broker", lambda: broker)

    text = npu_service.format_status()
    assert "ram_gate=1" in text
    assert "fell back for free RAM" not in text
