"""Latency and identity-cache contracts for capability evidence admission."""
from __future__ import annotations

import logging
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference import capability_evidence, capability_refresh
from sonder_runtime.adapters.inference.capability_evidence import (
    load_production_evidence,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse
from sonder_runtime.application.routing.evidence_gateway import (
    CapabilityEvidenceGateway,
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


def _identity(model="primary", **changes):
    return replace(BackendIdentity("ollama", model, "a" * 64, "Q4", "1",
                                   "b" * 64, "c" * 64, 8192, "local"), **changes)


def _record(model="primary", *, passed=(Cap.STRUCTURED,), failed=(), checked_at=None,
            synthetic=False, identity=None):
    results = [] if Cap.CHAT in (*passed, *failed) else [ProbeResult(Cap.CHAT, True, "passed")]
    results.extend(ProbeResult(capability, True, "passed") for capability in passed)
    results.extend(ProbeResult(capability, False, "failed") for capability in failed)
    return BackendConformanceRecord(
        "ollama", model, time.time() if checked_at is None else checked_at,
        tuple(results), synthetic=synthetic, identity=identity,
    )


class _Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


class _Gateway:
    def __init__(self, model="primary"):
        self.route = SimpleNamespace(provider_id="ollama", model=model, cloud=False)
        self.responses = 0

    def resolve_route(self, request, context):
        return self.route

    def generate(self, request, context):
        self.responses += 1
        return ModelResponse("decision", self.route.model, request.tier)


def _request(model="primary", **options):
    return ModelRequest("make a structured decision", model,
                        options={"format": {"type": "object"}, **options})


def _context():
    return local_owner_context(correlation_id="identity-cache")


def _gateway(store, identity, *, mode="advisory", clock=None, model="primary"):
    calls = []

    def observe(route, payload):
        calls.append((route.model, payload.get("options", {}).get("num_ctx")))
        return identity(route.model)

    kwargs = {"mode": mode}
    if clock is not None:
        kwargs["clock"] = clock
    return (CapabilityEvidenceGateway(_Gateway(model), store, observe, **kwargs), calls)


def test_advisory_empty_store_has_zero_identity_calls_across_structured_requests(tmp_path):
    gateway, calls = _gateway(load_production_evidence(tmp_path), _identity)
    for _ in range(5):
        gateway.generate(_request(), _context())
    assert calls == []


def test_advisory_all_pass_and_unknown_evidence_have_zero_identity_calls(tmp_path):
    store = load_production_evidence(tmp_path)
    store.save(_record(identity=_identity()))
    gateway, calls = _gateway(store, _identity)
    for _ in range(4):
        gateway.generate(_request(), _context())
    assert calls == []

    unknown_store = load_production_evidence(tmp_path / "unknown")
    unknown_store.save(_record(passed=(), identity=_identity()))
    gateway, calls = _gateway(unknown_store, _identity)
    for _ in range(4):
        gateway.generate(_request(), _context())
    assert calls == []


# Records are built when the test runs, not at collection: a timestamp taken at
# collection time drifts across a long suite (a "future" record becomes fresh).
@pytest.mark.parametrize("make_record", [
    lambda: _record(passed=(), failed=(Cap.STRUCTURED,), checked_at=time.time() - 86_401,
                    identity=_identity()),
    lambda: _record(passed=(), failed=(Cap.STRUCTURED,), checked_at=time.time() + 3_600,
                    identity=_identity()),
    lambda: _record(passed=(), failed=(Cap.STRUCTURED,), synthetic=True, identity=_identity()),
    lambda: _record(passed=(), failed=(Cap.STRUCTURED,), identity=None),
    lambda: _record(passed=(Cap.STRUCTURED,), failed=(Cap.CHAT,), identity=_identity()),
], ids=["stale", "future", "synthetic", "no-identity", "irrelevant-failure"])
def test_advisory_unusable_or_irrelevant_failures_have_zero_identity_calls(tmp_path, make_record):
    store = load_production_evidence(tmp_path)
    store.save(make_record())
    gateway, calls = _gateway(store, _identity)
    for _ in range(3):
        gateway.generate(_request(), _context())
    assert calls == []


def test_advisory_relevant_failure_observes_once_and_reuses_within_ttl(tmp_path):
    store = load_production_evidence(tmp_path)
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity()))
    clock = _Clock()
    gateway, calls = _gateway(store, _identity, clock=clock)
    gateway.generate(_request(), _context())
    gateway.generate(_request(), _context())
    assert len(calls) == 1
    clock.value += 60.0
    gateway.generate(_request(), _context())
    assert len(calls) == 2


def test_strict_observes_once_per_ttl_window_for_pre_and_post_dispatch(tmp_path):
    store = load_production_evidence(tmp_path)
    store.save(_record(identity=_identity()))
    clock = _Clock()
    gateway, calls = _gateway(store, _identity, mode="strict", clock=clock)
    gateway.generate(_request(), _context())
    gateway.generate(_request(), _context())
    assert len(calls) == 1
    clock.value += 60.0
    gateway.generate(_request(), _context())
    assert len(calls) == 2


@pytest.mark.parametrize("failure", [None, RuntimeError("probe unavailable")])
def test_unavailable_identity_is_negative_cached(tmp_path, failure):
    store = load_production_evidence(tmp_path)
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity()))
    calls = []

    def observe(*_):
        calls.append(1)
        if failure is not None:
            raise failure

    gateway = CapabilityEvidenceGateway(_Gateway(), store, observe)
    gateway.generate(_request(), _context())
    gateway.generate(_request(), _context())
    assert len(calls) == 1


@pytest.mark.parametrize("invalidate", ["ttl", "refresh"])
def test_strict_detects_identity_change_after_dispatch(tmp_path, invalidate):
    store = load_production_evidence(tmp_path)
    store.save(_record(identity=_identity()))
    clock = _Clock()
    changed = [False]

    def observe(*_):
        return _identity(backend_version="2") if changed[0] else _identity()

    inner = _Gateway()
    def dispatch(request, context):
        changed[0] = True
        if invalidate == "ttl":
            clock.value += 60.0
        else:
            load_production_evidence(tmp_path).save(_record(identity=_identity()))
        return ModelResponse("decision", "primary", request.tier)
    inner.generate = dispatch
    gateway = CapabilityEvidenceGateway(inner, store, observe, mode="strict", clock=clock)
    with pytest.raises(DependencyUnavailable, match="identity changed"):
        gateway.generate(_request(), _context())


def test_production_identity_cache_isolates_origin_and_effective_context(tmp_path, monkeypatch):
    store = load_production_evidence(tmp_path)
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity()))
    calls = []
    default_context = [8192]
    monkeypatch.setattr(capability_evidence.context_policy, "default_requested",
                        lambda: default_context[0])

    def observe(origin, model, payload):
        calls.append((origin, model, capability_evidence.identity_context_tokens(payload)))
        return _identity(model)

    monkeypatch.setattr(capability_evidence, "ollama_identity", observe)
    gateway = CapabilityEvidenceGateway(
        _Gateway(), store, capability_evidence.request_identity,
        identity_key_for=capability_evidence.request_identity_key, clock=_Clock(),
    )
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
    gateway.generate(_request(), _context())
    gateway.generate(_request(), _context())
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11435")
    gateway.generate(_request(), _context())
    gateway.generate(_request(options={"num_ctx": 4096}), _context())
    default_context[0] = 4096
    gateway.generate(_request(), _context())
    assert calls == [
        ("http://127.0.0.1:11434", "primary", 8192),
        ("http://127.0.0.1:11435", "primary", 8192),
        ("http://127.0.0.1:11435", "primary", 4096),
    ]


def test_advisory_does_not_reobserve_after_slow_dispatch(tmp_path):
    store = load_production_evidence(tmp_path)
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity()))
    clock = _Clock()
    gateway, calls = _gateway(store, _identity, clock=clock)

    def dispatch(request, context):
        clock.value += 60.0
        return ModelResponse("decision", "primary", request.tier)

    gateway._gateway.generate = dispatch
    assert gateway.generate(_request(), _context()).text == "decision"
    assert len(calls) == 1


def test_refresh_cli_revision_invalidates_cache_and_failed_refresh_stays_unknown(tmp_path, monkeypatch):
    store = load_production_evidence(tmp_path)
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity()))
    gateway, calls = _gateway(store, _identity)
    gateway.generate(_request(), _context())
    assert len(calls) == 1

    class MeasuredProbe:
        def __init__(self, origin, model, context_tokens):
            self.model = model

        def run(self, timeout_seconds):
            return _record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity("primary", backend_version="2"))

    monkeypatch.setattr(capability_refresh, "configured_local_models", lambda home=None: ("primary",))
    monkeypatch.setattr(capability_refresh, "OllamaConformanceProbe", MeasuredProbe)
    capability_refresh.refresh_capabilities(origin="http://127.0.0.1:11434", home=tmp_path,
                                            models=("primary",))
    gateway.generate(_request(), _context())
    assert len(calls) == 2

    class FailedProbe(MeasuredProbe):
        def run(self, timeout_seconds):
            raise RuntimeError("probe failed")

    monkeypatch.setattr(capability_refresh, "OllamaConformanceProbe", FailedProbe)
    capability_refresh.refresh_capabilities(origin="http://127.0.0.1:11434", home=tmp_path,
                                            models=("primary",))
    gateway.generate(_request(), _context())
    assert len(calls) == 2


def test_refresh_revision_invalidates_cached_identity(tmp_path):
    store = load_production_evidence(tmp_path)
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity()))
    clock = _Clock()
    gateway, calls = _gateway(store, _identity, clock=clock)
    gateway.generate(_request(), _context())
    assert len(calls) == 1
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity(backend_version="2")))
    gateway.generate(_request(), _context())
    assert len(calls) == 2


@pytest.mark.parametrize("mode,raises", [("advisory", False), ("strict", True)])
def test_model_mismatch_warns_advisory_and_raises_strict(tmp_path, caplog, mode, raises):
    store = load_production_evidence(tmp_path)
    store.save(_record(identity=_identity()))
    inner = _Gateway()
    inner.generate = lambda request, context: ModelResponse("decision", "other", request.tier)
    gateway = CapabilityEvidenceGateway(inner, store, lambda *_: _identity(), mode=mode)
    with caplog.at_level(logging.WARNING):
        if raises:
            with pytest.raises(DependencyUnavailable, match="different model"):
                gateway.generate(_request(), _context())
        else:
            assert gateway.generate(_request(), _context()).model == "other"
            assert "different model" in caplog.text


@pytest.mark.parametrize(("route_model", "response_model"), [
    ("namespace/model", "namespace/model:latest"),
    ("namespace/model:latest", "namespace/model"),
])
def test_ollama_latest_alias_is_accepted_in_advisory_and_strict(
    tmp_path, route_model, response_model,
):
    for mode in ("advisory", "strict"):
        store = load_production_evidence(
            tmp_path / mode / route_model.replace("/", "_").replace(":", "_")
        )
        store.save(_record(model=route_model, identity=_identity(route_model)))
        inner = _Gateway(route_model)
        inner.generate = lambda request, context: ModelResponse(
            "decision", response_model, request.tier)
        gateway = CapabilityEvidenceGateway(inner, store, lambda *_: _identity(route_model), mode=mode)
        assert gateway.generate(_request(route_model), _context()).model == response_model


def test_identity_cache_key_isolated_by_context_and_model(tmp_path):
    store = load_production_evidence(tmp_path)
    store.save(_record(passed=(), failed=(Cap.STRUCTURED,), identity=_identity()))
    store.save(_record("alternate", passed=(), failed=(Cap.STRUCTURED,), identity=_identity("alternate")))
    clock = _Clock()
    gateway, calls = _gateway(store, _identity, clock=clock)
    gateway.generate(_request(), _context())
    gateway.generate(_request(num_ctx=4096), _context())
    gateway._gateway.route = SimpleNamespace(provider_id="ollama", model="alternate", cloud=False)
    gateway.generate(_request("alternate", num_ctx=4096), _context())
    assert len(calls) == 3
