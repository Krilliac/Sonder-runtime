"""Physical-worker availability under advisory, strict and disabled evidence."""
import time
from dataclasses import replace

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerCapabilityUnavailable,
)
from sonder_runtime.application.routing.backend_conformance import (
    RecentCapabilityEvidence,
)
from sonder_runtime.domain.routing.backend_conformance import (
    BackendCapability as Cap,
)
from sonder_runtime.domain.routing.backend_conformance import (
    BackendConformanceRecord,
    BackendIdentity,
    ProbeResult,
)

PRIMARY = "http://127.0.0.1:11434"
SECOND = "http://127.0.0.2:11434"


def identity(origin=PRIMARY):
    return BackendIdentity("ollama", "m", "a" * 64, "Q4", "1", "b" * 64,
                           "c" * 64, 8192, origin)


def fail(store, **changes):
    record = BackendConformanceRecord("ollama", "m", time.time(), (
        ProbeResult(Cap.CHAT, True, "passed"),
        ProbeResult(Cap.STRUCTURED, False, "failed"),
    ), identity=identity())
    store.save(replace(record, **changes))


def pool_for(tmp_path, *, mode="advisory", workers=(), identity_for=None):
    store = RecentCapabilityEvidence(tmp_path / "capabilities.json")
    pool = OllamaWorkerPool(
        PRIMARY, workers, recent_evidence=store, capability_routing=mode,
        identity_for=identity_for or (lambda origin, *_: identity(origin)),
        capability_prober=lambda _: {"models": ["m"]},
    )
    pool.refresh_capabilities()
    return pool, store


@pytest.mark.parametrize("method", ["request", "request_primary"])
@pytest.mark.parametrize("payload", [
    {"format": {"type": "object"}}, {"response_format": {"type": "json_object"}},
    {"messages": [{"content": [{"type": "input_image"}]}]},
    {"messages": [{"content": "x" * 24576}]},
    {"tools": ["echo"], "format": {"type": "object"}},
])
def test_empty_store_retains_primary(tmp_path, method, payload, caplog):
    pool, _ = pool_for(tmp_path)
    with caplog.at_level("INFO"):
        assert getattr(pool, method)(lambda origin: origin, model="m", payload=payload) == PRIMARY
    assert "unverified" in caplog.text
    assert "fallback_used" not in caplog.text


def test_failed_primary_uses_unverified_other_identity(tmp_path, caplog):
    pool, store = pool_for(tmp_path, workers=(SECOND,))
    fail(store)
    with caplog.at_level("INFO"):
        assert pool.request(lambda origin: origin, model="m", structured_output=True) == SECOND
    assert "unverified" in caplog.text
    assert "fallback_used" not in caplog.text


@pytest.mark.parametrize("mode", ["advisory", "strict"])
@pytest.mark.parametrize("method", ["request", "request_primary"])
def test_all_failed_fallback_or_refusal(tmp_path, mode, method, caplog):
    pool, store = pool_for(tmp_path, mode=mode)
    fail(store)
    calls = []
    kwargs = {"model": "m", "payload": {"format": {"type": "object"}}}
    if mode == "strict":
        with pytest.raises(WorkerCapabilityUnavailable):
            getattr(pool, method)(lambda origin: calls.append(origin), **kwargs)
        assert not calls
    else:
        assert getattr(pool, method)(lambda origin: origin, **kwargs) == PRIMARY
        assert "fallback_used" in caplog.text and "failed" in caplog.text
    assert pool._states[0].inflight == 0


@pytest.mark.parametrize("method", ["request", "request_primary"])
def test_off_never_reads_evidence_or_identity(tmp_path, monkeypatch, method):
    def unexpected(*_args, **_kwargs):
        pytest.fail("off mode inspected capability evidence or identity")

    pool, store = pool_for(tmp_path, mode="off", identity_for=unexpected)
    fail(store)
    monkeypatch.setattr(store, "assess", unexpected)
    assert getattr(pool, method)(lambda origin: origin, model="m", payload={"format": "json"}) == PRIMARY


@pytest.mark.parametrize("changes", [{"checked_at": 1}, {"synthetic": True},
                                     {"identity": replace(identity(), backend_version="old")}])
def test_unverified_failures_do_not_trigger_fallback(tmp_path, changes, caplog):
    pool, store = pool_for(tmp_path)
    fail(store, **changes)
    with caplog.at_level("INFO"):
        assert pool.request(lambda origin: origin, model="m", structured_output=True) == PRIMARY
    assert "unverified" in caplog.text
    assert "fallback_used" not in caplog.text


@pytest.mark.parametrize("mode", ["advisory", "strict"])
def test_identity_discovery_failure_is_unknown(tmp_path, mode, caplog):
    def unavailable(*_args):
        raise RuntimeError("identity unavailable")

    pool, _ = pool_for(tmp_path, mode=mode, identity_for=unavailable)
    if mode == "strict":
        with pytest.raises(WorkerCapabilityUnavailable):
            pool.request(lambda _: pytest.fail("strict dispatched"), model="m", structured_output=True)
    else:
        with caplog.at_level("INFO"):
            assert pool.request(lambda origin: origin, model="m", structured_output=True) == PRIMARY
        assert "unverified" in caplog.text
    assert pool._states[0].inflight == 0


@pytest.mark.parametrize("mode", ["advisory", "strict"])
def test_zero_identity_budget(tmp_path, mode, caplog):
    pool, _ = pool_for(tmp_path, mode=mode)
    if mode == "strict":
        with pytest.raises(WorkerCapabilityUnavailable):
            pool.request(lambda _: pytest.fail("strict dispatched"), model="m",
                         structured_output=True, admission_timeout_seconds=0)
    else:
        with caplog.at_level("INFO"):
            assert pool.request(lambda origin: origin, model="m", structured_output=True,
                                admission_timeout_seconds=0) == PRIMARY
        assert "unverified" in caplog.text
        assert "fallback_used" not in caplog.text


def test_failure_at_recheck_tries_alternative(tmp_path):
    observations = []
    def observe(origin, *_args):
        observations.append(origin)
        if observations.count(PRIMARY) == 2:
            fail(store)
        return identity(origin)

    pool, store = pool_for(tmp_path, workers=(SECOND,), identity_for=observe)
    # Stable equal scores make the configured primary the first selection.
    for state in pool._states:
        state.latency_ewma_ms = 1.0
    assert pool.request(lambda origin: origin, model="m", structured_output=True) == SECOND
    assert all(state.inflight == 0 for state in pool._states)


@pytest.mark.parametrize("mode", ["advisory", "strict", "off"])
def test_factory_resolves_production_mode(tmp_path, monkeypatch, mode):
    from sonder_runtime.adapters.inference import ollama_pool

    monkeypatch.setenv("SONDER_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("SONDER_CAPABILITY_ROUTING", mode)
    monkeypatch.setattr(ollama_pool, "_configured_pool", None)
    pool = ollama_pool.from_environment(PRIMARY)
    assert pool._capability_routing == mode
    assert pool._recent_evidence.path == tmp_path / "capability_evidence.json"


@pytest.mark.parametrize("method", ["request", "request_primary"])
@pytest.mark.parametrize("before,after", [
    (identity(), replace(identity(), backend_version="2")), (None, identity()), (identity(), None),
])
def test_advisory_identity_integrity_does_not_require_observations(tmp_path, method, before, after):
    observed = [before]
    pool, _ = pool_for(tmp_path, identity_for=lambda *_: observed[0])
    calls = []

    def send(origin):
        calls.append(origin)
        observed[0] = after
        return "answer"

    kwargs = {"model": "m", "payload": {"format": "json"}}
    if before is not None and after is not None:
        with pytest.raises(WorkerCapabilityUnavailable, match="identity.changed"):
            getattr(pool, method)(send, **kwargs)
    else:
        assert getattr(pool, method)(send, **kwargs) == "answer"
    assert calls == [PRIMARY]
    assert pool._states[0].inflight == 0
