"""Strict admission, production composition and operator refresh contracts."""
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference import capability_refresh
from sonder_runtime.adapters.inference.capability_evidence import (
    load_production_evidence,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.application.routing.evidence_gateway import (
    IDENTITY_CACHE_TTL_SECONDS,
    CapabilityEvidenceGateway,
)
from sonder_runtime.application.routing.request_capabilities import (
    check_request_evidence,
    request_requirements,
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


def identity(**changes):
    return replace(BackendIdentity("ollama", "fixture:latest", "a" * 64, "Q4",
                                   "1.0", "b" * 64, "c" * 64, 8192, "fixture"), **changes)


def record(*capabilities, checked_at=None, synthetic=False):
    return BackendConformanceRecord(
        "ollama", "fixture:latest", time.time() if checked_at is None else checked_at,
        tuple(ProbeResult(cap, True, "measured") for cap in (Cap.CHAT, *capabilities)),
        synthetic=synthetic, identity=identity(),
    )


@pytest.mark.parametrize("contents", [None, "broken JSON", '{"schema":1,"records":{}}'])
def test_missing_or_corrupt_evidence_keeps_plain_text_but_refuses_tools(tmp_path, contents):
    evidence = load_production_evidence(tmp_path)
    if contents is not None:
        evidence.path.write_text(contents, encoding="utf-8")
    assert check_request_evidence(evidence, "fixture:latest", frozenset(), mode="strict")[0]
    allowed, reason = check_request_evidence(evidence, "fixture:latest", {Cap.TOOL_NATIVE},
                                             identity=identity(), mode="strict")
    assert not allowed
    assert "tool_native" in reason and "missing" in reason


@pytest.mark.parametrize("changes", [{"model_digest": "d" * 64}, {"backend_version": "2.0"}])
def test_model_or_ollama_update_invalidates_passing_evidence(tmp_path, changes):
    evidence = load_production_evidence(tmp_path)
    evidence.save(record(Cap.TOOL_NATIVE))
    assert check_request_evidence(evidence, "fixture:latest", {Cap.TOOL_NATIVE}, identity=identity(), mode="strict")[0]
    allowed, reason = check_request_evidence(evidence, "fixture:latest", {Cap.TOOL_NATIVE},
                                             identity=identity(**changes), mode="strict")
    assert not allowed and "identity_changed" in reason


@pytest.mark.parametrize("old_record,reason", [
    (record(Cap.STRUCTURED, checked_at=1), "stale"),
    (record(Cap.STRUCTURED, synthetic=True), "synthetic"),
    (record(), "missing"),
])
def test_nonpassing_evidence_cannot_admit_features(tmp_path, old_record, reason):
    evidence = load_production_evidence(tmp_path)
    evidence.save(old_record)
    assert check_request_evidence(evidence, "fixture:latest", set(), mode="strict")[0]
    allowed, detail = check_request_evidence(evidence, "fixture:latest", {Cap.STRUCTURED}, identity=identity(), mode="strict")
    assert not allowed and reason in detail


def test_request_requires_combined_tools_schema_image_and_long_input():
    required = request_requirements({"tools": [{"type": "function"}], "format": {"type": "object"},
                                     "messages": [{"role": "user", "content": "x" * 25000, "images": ["png"]}]})
    assert required == {Cap.TOOL_NATIVE, Cap.STRUCTURED, Cap.TOOLS_WITH_SCHEMA, Cap.VISION, Cap.LONG_CONTEXT}
    assert not request_requirements({"prompt": "hello", "options": {"num_ctx": 32768}})


@pytest.mark.parametrize("capability", [Cap.TOOL_NATIVE, Cap.STRUCTURED, Cap.VISION, Cap.LONG_CONTEXT])
def test_each_requested_feature_requires_its_own_passing_probe(tmp_path, capability):
    evidence = load_production_evidence(tmp_path)
    evidence.save(record())
    assert not check_request_evidence(evidence, "fixture:latest", {capability}, identity=identity(), mode="strict")[0]
    evidence.save(record(capability))
    assert check_request_evidence(evidence, "fixture:latest", {capability}, identity=identity(), mode="strict")[0]
    failed = replace(record(), results=(ProbeResult(Cap.CHAT, True, "passed"),
                                       ProbeResult(capability, False, "failed")))
    evidence.save(failed)
    assert not check_request_evidence(evidence, "fixture:latest", {capability}, identity=identity(), mode="strict")[0]


def test_individual_tool_and_schema_passes_do_not_admit_combined_request(tmp_path):
    evidence = load_production_evidence(tmp_path)
    required = request_requirements({"tools": ["echo"], "format": {"type": "object"}})
    evidence.save(record(Cap.TOOL_NATIVE, Cap.STRUCTURED))
    assert not check_request_evidence(evidence, "fixture:latest", required, identity=identity(), mode="strict")[0]
    failed = record(Cap.TOOL_NATIVE, Cap.STRUCTURED)
    evidence.save(replace(failed, results=(*failed.results,
                        ProbeResult(Cap.TOOLS_WITH_SCHEMA, False, "grammar_masked_tool"))))
    assert not check_request_evidence(evidence, "fixture:latest", required, identity=identity(), mode="strict")[0]
    evidence.save(record(Cap.TOOL_NATIVE, Cap.STRUCTURED, Cap.TOOLS_WITH_SCHEMA))
    assert check_request_evidence(evidence, "fixture:latest", required, identity=identity(), mode="strict")[0]


def test_gateway_filters_before_dispatch_and_detects_identity_change(tmp_path):
    evidence = load_production_evidence(tmp_path)
    observed = [identity()]
    clock = [0.0]
    calls = []
    route = SimpleNamespace(provider_id="ollama", model="fixture:latest", cloud=False)

    class Gateway:
        def resolve_route(self, request, context):
            return route

        def generate(self, request, context):
            calls.append(request)
            return ModelResponse("ok", route.model, request.tier)

    gateway = CapabilityEvidenceGateway(Gateway(), evidence, lambda *_: observed[0], mode="strict",
                                        clock=lambda: clock[0])
    context = local_owner_context(correlation_id="production-evidence")
    assert gateway.generate(ModelRequest("hello", "code"), context).text == "ok"
    request = ModelRequest("echo alpha", "code", options={"tools": ["echo"]})
    with pytest.raises(DependencyUnavailable, match="missing"):
        gateway.generate(request, context)
    assert len(calls) == 1
    evidence.save(record(Cap.TOOL_NATIVE))
    assert gateway.generate(request, context).text == "ok"
    assert calls[-1]._resolved_route is route
    observed[0] = identity(backend_version="2")
    clock[0] += IDENTITY_CACHE_TTL_SECONDS
    with pytest.raises(DependencyUnavailable, match="identity_changed"):
        gateway.generate(request, context)
    assert len(calls) == 2


def test_bootstrap_always_supplies_production_store(tmp_path, monkeypatch):
    from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
    from sonder_runtime.bootstrap import main as entry

    built = []
    monkeypatch.setattr(entry, "build_config_from_env", lambda _: RuntimeConfig(sonder_home=str(tmp_path)))
    monkeypatch.setattr(entry.caps, "freeze", lambda value: value)
    monkeypatch.setattr(entry, "build_runtime", lambda *args, **kwargs: built.append(kwargs))
    assert entry.main([]) == 0
    assert built[0]["route_evidence"].path == tmp_path / "capability_evidence.json"
    assert built[0]["production_route_policy"] is True
    assert not (tmp_path / "capability_evidence.json").exists()


def test_refresh_persists_and_failed_refresh_invalidates_old_success(tmp_path, monkeypatch):
    monkeypatch.setattr(capability_refresh, "configured_local_models", lambda _: ("fixture:latest",))
    fail = [False]

    class Probe:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, **kwargs):
            if fail[0]:
                raise OSError("sensitive transport detail")
            return record(Cap.TOOL_NATIVE, Cap.TOOLS_WITH_SCHEMA)

    monkeypatch.setattr(capability_refresh, "OllamaConformanceProbe", Probe)
    result = capability_refresh.refresh_capabilities(origin="http://127.0.0.1:11434", home=tmp_path)
    assert result["models"][0]["ollama_version"] == "1.0"
    assert load_production_evidence(tmp_path).load("ollama", "fixture:latest").synthetic is False
    fail[0] = True
    result = capability_refresh.refresh_capabilities(origin="http://127.0.0.1:11434", home=tmp_path)
    assert result["models"][0]["status"] == "unavailable"
    assert "sensitive" not in str(result)
    assert load_production_evidence(tmp_path).load("ollama", "fixture:latest").synthetic is True


def test_refresh_cli_passes_configured_home_and_bounded_options(tmp_path, monkeypatch, capsys):
    from sonder_runtime import __main__ as cli

    observed = []
    monkeypatch.setattr(cli, "_load_config", lambda _: SimpleNamespace(
        ollama=SimpleNamespace(url="http://127.0.0.1:11434"),
        state=SimpleNamespace(home=str(tmp_path)),
    ))
    monkeypatch.setattr(capability_refresh, "refresh_capabilities", lambda **kw: (
        observed.append(kw) or {"models": [{"status": "measured"}]}
    ))
    assert cli.main(["capabilities", "refresh", "--model", "fixture:latest",
                     "--timeout", "30", "--context-tokens", "8192", "--json"]) == 0
    assert observed[0]["home"] == str(tmp_path)
    assert observed[0]["models"] == ["fixture:latest"]
    assert observed[0]["timeout_seconds"] == 30
    assert "measured" in capsys.readouterr().out


def test_production_container_keeps_plain_text_available_and_gates_features(tmp_path, monkeypatch):
    import sonder_runtime.adapters.inference.model_gateway_factory as factory
    from sonder_runtime.adapters.runtime_capabilities import RuntimeCapabilities
    from sonder_runtime.adapters.runtime_configuration import RuntimeConfig
    from sonder_runtime.adapters.runtime_container import build_runtime

    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", "strict")

    class Gateway:
        def generate(self, request, context):
            assert not request.options
            return ModelResponse("still available", "fixture:latest", request.tier)

    monkeypatch.setattr(factory, "build_model_gateway", lambda _: Gateway())
    runtime = build_runtime(RuntimeConfig(), RuntimeCapabilities(),
                            route_evidence=load_production_evidence(tmp_path), production_route_policy=True)
    context = local_owner_context(correlation_id="production-composition")
    assert runtime.model_gateway.generate(ModelRequest("hello", "code"), context).text == "still available"
    with pytest.raises(DependencyUnavailable, match="identity_missing"):
        runtime.model_gateway.generate(ModelRequest("echo", "code", options={"tools": ["echo"]}), context)


def test_production_pool_factory_opens_store_without_creating_it(tmp_path, monkeypatch):
    from sonder_runtime.adapters.inference import ollama_pool

    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(ollama_pool, "_configured_pool", None)
    pool = ollama_pool.from_environment("http://127.0.0.1:11434")
    assert pool._recent_evidence.path == tmp_path / "capability_evidence.json"
    assert pool._identity_for is not None
    assert not pool._recent_evidence.path.exists()


def test_refresh_refuses_unconfigured_model_before_probe_or_write(tmp_path, monkeypatch):
    monkeypatch.setattr(capability_refresh, "configured_local_models", lambda _: ("fixture:latest",))
    with pytest.raises(ValueError, match="configured local tiers"):
        capability_refresh.refresh_capabilities(origin="http://127.0.0.1:11434", home=tmp_path,
                                                models=["cloud-model"])
    assert not load_production_evidence(tmp_path).path.exists()
