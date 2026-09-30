"""Capability evidence gates specialised tier and Ollama selections."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

import tier_router
from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerCapabilityUnavailable,
)
from sonder_runtime.application.routing.backend_conformance import (
    RecentCapabilityEvidence,
)
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability,
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)


def _identity(model: str, *, digest: str = "a", version: str = "0.1") -> BackendIdentity:
    return BackendIdentity("ollama", model, (digest * 64)[:64], "Q4_K_M", version, "b" * 64, "c" * 64, 8192, "local")


def _save(path: Path, model: str, identity: BackendIdentity, *, passed=(), checked_at=None):
    results = tuple(ProbeResult(capability, True, "passed") for capability in {BackendCapability.CHAT, *passed})
    store = RecentCapabilityEvidence(path)
    store.save(BackendConformanceRecord("ollama", model, time.time() if checked_at is None else checked_at, results, probe_version=3, identity=identity))
    return store


def test_tier_prefers_passing_alternate_when_preferred_failed(tmp_path):
    preferred, alternate = _identity("preferred"), _identity("alternate")
    store = _save(tmp_path / "evidence.json", "preferred", preferred)
    store.save(BackendConformanceRecord("ollama", "preferred", time.time(), (
        ProbeResult(BackendCapability.CHAT, True, "passed"), ProbeResult(BackendCapability.TOOL_NATIVE, False, "probe_failed")), probe_version=3, identity=preferred))
    store.save(BackendConformanceRecord("ollama", "alternate", time.time(), (
        ProbeResult(BackendCapability.CHAT, True, "passed"), ProbeResult(BackendCapability.TOOL_NATIVE, True, "passed")), probe_version=3, identity=alternate))
    result = tier_router.route("call the tool", {"code", "reasoning"}, recent_evidence=store,
                              tier_models={"code": "preferred", "reasoning": "alternate"}, tools=True,
                              identity_for=lambda model: {"preferred": preferred, "alternate": alternate}[model])
    assert result["tier"] == "reasoning"
    assert "evidence" in result["reason"]


def test_tier_reason_reports_stale_and_identity_change(tmp_path):
    model = "model:latest"
    stale = _save(tmp_path / "stale.json", model, _identity(model), passed=(BackendCapability.TOOL_NATIVE,), checked_at=0)
    result = tier_router.route("call the tool", {"code"}, recent_evidence=stale,
                               tier_models={"code": model}, tools=True, identity_for=lambda _model: _identity(model), capability_routing="strict")
    assert result["tier"] is None
    assert "stale" in result["reason"]
    current = _save(tmp_path / "changed.json", model, _identity(model), passed=(BackendCapability.TOOL_NATIVE,))
    result = tier_router.route("call the tool", {"code"}, recent_evidence=current,
                               tier_models={"code": model}, tools=True,
                               identity_for=lambda _model: _identity(model, digest="d", version="9.9"), capability_routing="strict")
    assert result["tier"] is None
    assert "identity_changed" in result["reason"]


def test_plain_text_keeps_semantic_route_without_evidence():
    result = tier_router.route("hello", {"code", "fast"}, semantic_enabled=True,
                               semantic_classifier=lambda _prompt: {"tier": "fast", "margin": 0.4, "model": "embed"})
    assert result["tier"] == "fast"
    assert result["signal"] == "semantic"


def test_pool_derives_tools_and_schema_from_payload_and_refuses(tmp_path):
    model, identity = "model:latest", _identity("model:latest")
    store = _save(tmp_path / "pool.json", model, identity)
    store.save(BackendConformanceRecord("ollama", model, time.time(), (
        ProbeResult(BackendCapability.CHAT, True, "passed"), ProbeResult(BackendCapability.TOOL_NATIVE, False, "failed"),
        ProbeResult(BackendCapability.STRUCTURED, True, "passed"), ProbeResult(BackendCapability.TOOLS_WITH_SCHEMA, False, "failed")), probe_version=3, identity=identity))
    pool = OllamaWorkerPool("http://127.0.0.1:11434", capability_prober=lambda _origin: {"protocol": "ollama-http-v1", "version": "1", "models": [model]}, recent_evidence=store, identity_for=lambda _origin, _model, _payload: identity, capability_routing="strict")
    with pytest.raises(WorkerCapabilityUnavailable, match="capabilities"):
        pool.request(lambda _origin: pytest.fail("refused worker selected"), model=model, payload={"tools": [{"type": "function"}], "format": {"type": "object"}})


def test_pool_primary_request_is_evidence_gated(tmp_path):
    model, identity = "model:latest", _identity("model:latest")
    store = _save(tmp_path / "primary.json", model, identity)
    pool = OllamaWorkerPool("http://127.0.0.1:11434", recent_evidence=store, identity_for=lambda _origin, _model, _payload: identity, capability_routing="strict")
    with pytest.raises(WorkerCapabilityUnavailable, match="primary capability route refused"):
        pool.request_primary(lambda _origin: pytest.fail("primary bypassed evidence"), model=model, payload={"tools": [{"type": "function"}]})


def test_pool_rechecks_identity_after_response_and_releases_slot(tmp_path):
    model, initial, changed = "model:latest", _identity("model:latest"), _identity("model:latest", digest="d")
    store = _save(tmp_path / "rotation.json", model, initial, passed=(BackendCapability.TOOL_NATIVE,))
    current = [initial]
    pool = OllamaWorkerPool("http://127.0.0.1:11434", capability_prober=lambda _origin: {"protocol": "ollama-http-v1", "version": "1", "models": [model]}, recent_evidence=store, identity_for=lambda _origin, _model, _payload: current[0], capability_routing="strict")
    def send(_origin):
        current[0] = changed
        _save(tmp_path / "rotation.json", model, changed, passed=(BackendCapability.TOOL_NATIVE,))
        return {"ok": True}
    with pytest.raises(WorkerCapabilityUnavailable, match="changed after response"):
        pool.request(send, model=model, payload={"tools": [{"type": "function"}]})
    assert pool._states[0].inflight == 0
