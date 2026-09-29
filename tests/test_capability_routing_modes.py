"""Availability contracts for production request evidence policy."""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

import tier_router
from sonder_runtime.adapters.inference.capability_evidence import (
    capability_routing_mode,
    load_production_evidence,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.application.routing.evidence_gateway import (
    CapabilityEvidenceGateway,
)
from sonder_runtime.application.routing.request_capabilities import (
    check_request_evidence,
)
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability as Cap,
)
from sonder_runtime.domain.routing.backend_conformance import (
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)


def identity(model="primary", **changes):
    return replace(BackendIdentity("ollama", model, "a" * 64, "Q4", "1",
                                   "b" * 64, "c" * 64, 8192, "local"), **changes)


def save_failure(store, model="primary", capability=Cap.STRUCTURED, **changes):
    record = BackendConformanceRecord(
        "ollama", model, time.time(),
        (ProbeResult(Cap.CHAT, True, "passed"), ProbeResult(capability, False, "failed")),
        identity=identity(model),
    )
    store.save(replace(record, **changes))


@pytest.mark.parametrize("payload", [
    {"format": {"type": "object"}}, {"response_format": {"type": "json_object"}},
    {"images": ["image"]}, {"messages": [{"role": "user", "content": "x" * 24576}]}, {"tools": ["echo"]},
    {"tools": ["echo"], "format": {"type": "object"}},
])
def test_empty_store_preserves_configured_route(tmp_path, payload):
    previous = tier_router.route("hello", ["code", "reasoning"], semantic_enabled=False)
    result = tier_router.route(
        "hello", ["code", "reasoning"], semantic_enabled=False,
        recent_evidence=load_production_evidence(tmp_path),
        tier_models={"code": "primary", "reasoning": "alternate"},
        identity_for=identity, request_payload=payload,
    )
    assert result["tier"] == previous["tier"]
    assert result["fallback_used"] == previous["fallback_used"]
    assert result["signal"] == previous["signal"]
    assert "unverified" in result["reason"]


def test_failure_uses_unverified_alternative(tmp_path):
    store = load_production_evidence(tmp_path)
    save_failure(store)
    result = tier_router.route(
        "hello", ["code", "reasoning"], recent_evidence=store,
        tier_models={"code": "primary", "reasoning": "alternate"},
        identity_for=identity, structured_output=True,
    )
    assert result["tier"] == "reasoning"
    assert result["fallback_used"]
    assert "failed" in result["reason"] and "unverified" in result["reason"]


@pytest.mark.parametrize("mode,expected", [("advisory", "code"), ("strict", None)])
def test_all_failed_falls_back_only_in_advisory(tmp_path, mode, expected):
    store = load_production_evidence(tmp_path)
    for model in ("primary", "alternate"):
        save_failure(store, model)
    result = tier_router.route(
        "hello", ["code", "reasoning"], recent_evidence=store,
        tier_models={"code": "primary", "reasoning": "alternate"},
        identity_for=identity, structured_output=True, capability_routing=mode,
    )
    assert result["tier"] == expected
    assert result["fallback_used"]
    assert "failed" in result["reason"]


@pytest.mark.parametrize("changes,current", [
    ({"checked_at": time.time() - 86401}, identity()),
    ({"synthetic": True}, identity()),
    ({}, identity(model_digest="d" * 64)),
    ({}, identity(backend_version="2")),
    ({}, None),
])
def test_unverified_failure_does_not_exclude(tmp_path, changes, current):
    store = load_production_evidence(tmp_path)
    save_failure(store, **changes)
    allowed, reason = check_request_evidence(store, "primary", {Cap.STRUCTURED}, identity=current)
    assert allowed and "unverified" in reason


def test_combined_request_unknown_then_measured_failure(tmp_path):
    store = load_production_evidence(tmp_path)
    required = {Cap.TOOL_NATIVE, Cap.STRUCTURED, Cap.TOOLS_WITH_SCHEMA}
    assert check_request_evidence(store, "primary", required, identity=identity())[0]
    save_failure(store, capability=Cap.TOOLS_WITH_SCHEMA)
    allowed, reason = check_request_evidence(store, "primary", required, identity=identity())
    assert not allowed and "failed" in reason


def test_off_does_not_read_identity_or_evidence():
    def unexpected(*_args, **_kwargs):
        pytest.fail("off mode must not inspect capability evidence or identity")

    result = tier_router.route(
        "hello", ["code"], recent_evidence=object(), tier_models={"code": "primary"},
        identity_for=unexpected, structured_output=True, capability_routing="off",
    )
    assert result["tier"] == "code"
    assert check_request_evidence(object(), "primary", {Cap.STRUCTURED}, mode="off")[0]


@pytest.mark.parametrize("value,expected", [(None, "advisory"), ("", "advisory"),
                                           ("strict", "strict"), (" OFF ", "off")])
def test_environment_mode(value, expected, monkeypatch):
    monkeypatch.delenv("SONDER_CAPABILITY_ROUTING", raising=False)
    if value is not None:
        monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", value)
    assert capability_routing_mode() == expected
    assert capability_routing_mode({}) == "advisory"


def test_invalid_environment_mode_is_reported(monkeypatch):
    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", "typo")
    with pytest.raises(ValueError, match="SONDER_CAPABILITY_ROUTING"):
        capability_routing_mode()


@pytest.mark.parametrize("mode", ["advisory", "strict", "off"])
def test_environment_controls_tier_policy(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", mode)
    result = tier_router.route("hello", ["code"], structured_output=True,
                               recent_evidence=load_production_evidence(tmp_path))
    assert result["tier"] == (None if mode == "strict" else "code")


@pytest.mark.parametrize("mode", ["advisory", "strict", "off"])
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("options", [{"format": {"type": "object"}}, {"images": ["image"]}])
def test_production_container_uses_mode_for_real_request_shapes(tmp_path, monkeypatch, caplog,
                                                               mode, failed, options):
    from sonder_runtime.adapters.inference import (
        capability_evidence,
        model_gateway_factory,
    )
    from sonder_runtime.adapters.runtime_capabilities import RuntimeCapabilities
    from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
    from sonder_runtime.adapters.runtime_container import build_runtime

    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", mode)
    calls = []
    observations = []
    route = SimpleNamespace(provider_id="ollama", model="primary", cloud=False)

    class Gateway:
        def resolve_route(self, request, context):
            return route

        def generate(self, request, context):
            calls.append(request)
            return ModelResponse("decision", "primary", request.tier)

    def observe(*_args):
        assert mode != "off"
        observations.append(1)
        return identity()

    store = load_production_evidence(tmp_path)
    if failed:
        save_failure(store, capability=Cap.VISION if "images" in options else Cap.STRUCTURED)
    monkeypatch.setattr(model_gateway_factory, "build_model_gateway", lambda _: Gateway())
    monkeypatch.setattr(capability_evidence, "request_identity", observe)
    runtime = build_runtime(RuntimeConfig(), RuntimeCapabilities(),
                            route_evidence=store, production_route_policy=True)
    request = ModelRequest("make a decision", "code", options=options)
    context = local_owner_context(correlation_id="capability-mode")
    with caplog.at_level("INFO"):
        if mode == "strict":
            with pytest.raises(DependencyUnavailable, match="capability route refused"):
                runtime.model_gateway.generate(request, context)
            assert not calls
        else:
            assert runtime.model_gateway.generate(request, context).text == "decision"
            assert calls[0].options == options
            if mode == "advisory":
                assert ("fallback_used" if failed else "unverified") in caplog.text
    assert len(observations) == int(mode == "strict" or (mode == "advisory" and failed))


@pytest.mark.parametrize("before,after", [
    (identity(), identity(backend_version="2")), (None, identity()), (identity(), None),
])
def test_advisory_does_not_observe_identity_without_relevant_failure(tmp_path, caplog,
                                                                  before, after):
    observed = [before]
    identity_calls = []
    route = SimpleNamespace(provider_id="ollama", model="primary", cloud=False)

    class Gateway:
        def resolve_route(self, request, context):
            return route

        def generate(self, request, context):
            observed[0] = after
            return ModelResponse("decision", "primary", request.tier)

    def observe(*_args):
        identity_calls.append(observed[0])
        return observed[0]

    gateway = CapabilityEvidenceGateway(Gateway(), load_production_evidence(tmp_path), observe)
    with caplog.at_level("INFO"):
        request = ModelRequest("hello", "code", options={"format": "json"})
        context = local_owner_context(correlation_id="identity-change")
        assert gateway.generate(request, context).text == "decision"
        assert "unverified" in caplog.text
        assert not identity_calls


def test_unverified_reason_survives_semantic_routing(tmp_path):
    result = tier_router.route(
        "hello", ["code", "fast"], semantic_enabled=True,
        semantic_classifier=lambda _: {"tier": "fast", "margin": 0.4, "model": "embed"},
        recent_evidence=load_production_evidence(tmp_path), structured_output=True,
    )
    assert result["tier"] == "fast" and result["signal"] == "semantic"
    assert "unverified" in result["reason"]


def test_only_requested_failure_excludes_advisory_route(tmp_path):
    store = load_production_evidence(tmp_path)
    store.save(BackendConformanceRecord(
        "ollama", "primary", time.time(),
        (ProbeResult(Cap.CHAT, False, "failed"), ProbeResult(Cap.STRUCTURED, True, "passed")),
        identity=identity(),
    ))
    assert check_request_evidence(store, "primary", {Cap.STRUCTURED}, identity=identity())[0]
    assert not check_request_evidence(store, "primary", {Cap.STRUCTURED},
                                      identity=identity(), mode="strict")[0]


@pytest.mark.parametrize("mode", ["advisory", "strict"])
def test_tier_identity_discovery_exception_is_unverified(tmp_path, mode):
    def unavailable(_model):
        raise RuntimeError("identity unavailable")

    result = tier_router.route(
        "hello", ["code"], recent_evidence=load_production_evidence(tmp_path),
        tier_models={"code": "primary"}, identity_for=unavailable,
        structured_output=True, capability_routing=mode,
    )
    assert result["tier"] == ("code" if mode == "advisory" else None)
    assert "unverified" in result["reason"]
