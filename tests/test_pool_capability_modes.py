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


def passing(store):
    store.save(BackendConformanceRecord(
        "ollama", "m", time.time(),
        (ProbeResult(Cap.CHAT, True, "passed"),
         ProbeResult(Cap.STRUCTURED, True, "passed")),
        identity=identity(),
    ))


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


def test_advisory_fresh_failure_observes_each_worker_once(tmp_path):
    calls = []
    pool, store = pool_for(
        tmp_path, workers=(SECOND,),
        identity_for=lambda origin, *_args: calls.append(origin) or identity(origin),
    )
    fail(store)
    assert pool.request(lambda origin: origin, model="m", structured_output=True) == SECOND
    assert calls == [PRIMARY, SECOND]


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


def test_advisory_does_not_recheck_identity_after_dispatch(tmp_path):
    observations = []
    pool, _ = pool_for(
        tmp_path, workers=(SECOND,),
        identity_for=lambda origin, *_args: observations.append(origin) or identity(origin),
    )
    # Stable equal scores make the configured primary the first selection.
    for state in pool._states:
        state.latency_ewma_ms = 1.0
    assert pool.request(lambda origin: origin, model="m", structured_output=True) == PRIMARY
    assert observations == []
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
    assert getattr(pool, method)(send, **kwargs) == "answer"
    assert calls == [PRIMARY]
    assert pool._states[0].inflight == 0


@pytest.mark.parametrize("record_kind", ["empty", "pass", "unrelated", "stale"])
@pytest.mark.parametrize("workers", [(), (SECOND,)])
@pytest.mark.parametrize("method", ["request", "request_primary"])
@pytest.mark.parametrize("payload", [
    {"format": "json"}, {"tools": ["echo"]}, {"images": ["fixture"]},
    {"messages": [{"content": "x" * 24576}]},
    {"tools": ["echo"], "format": "json"},
])
def test_advisory_pool_skips_identity_for_non_actionable_evidence(
    tmp_path, record_kind, workers, method, payload,
):
    pool, store = pool_for(tmp_path, workers=workers)
    if record_kind == "pass":
        passing(store)
    elif record_kind == "unrelated":
        fail(store)
        store.save(BackendConformanceRecord(
            "ollama", "m", time.time(),
            (ProbeResult(Cap.CHAT, False, "failed"),
             ProbeResult(Cap.STRUCTURED, True, "passed")),
            identity=identity(),
        ))
    elif record_kind == "stale":
        fail(store, checked_at=1)
    calls = []
    pool._identity_for = lambda *args: calls.append(args) or identity(args[0])
    result = getattr(pool, method)(lambda origin: origin, model="m", payload=payload)
    assert result in ({PRIMARY} if not workers else {PRIMARY, SECOND})
    assert calls == []


def test_pool_strict_identity_cache_covers_pre_and_post_dispatch(tmp_path):
    pool, store = pool_for(tmp_path, mode="strict")
    passing(store)
    calls = []
    pool._identity_for = lambda *args: calls.append(args) or identity(args[0])
    assert pool.request(lambda origin: origin, model="m", structured_output=True) == PRIMARY
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["advisory", "strict"])
@pytest.mark.parametrize("method,workers", [("request_primary", ()), ("request", (SECOND,))])
def test_pool_cache_reuses_then_expires_and_invalidates(tmp_path, mode, method, workers, monkeypatch):
    from sonder_runtime.adapters.inference import capability_evidence

    clock = [10.0]
    calls = []
    pool, store = pool_for(tmp_path, mode=mode, workers=workers)
    from sonder_runtime.application.routing.identity_cache import IdentityObservationCache

    pool._identity_cache = IdentityObservationCache(clock=lambda: clock[0])
    pool._identity_for = lambda origin, *_: calls.append(origin) or identity()
    (fail if mode == "advisory" else passing)(store)
    context = [8192]
    monkeypatch.setattr(capability_evidence.context_policy, "default_requested", lambda: context[0])
    request = getattr(pool, method)

    def send(origin):
        return origin

    for _ in range(2):
        request(send, model="m", payload={"format": "json"})
    per_window = 1 + len(workers)
    assert len(calls) == per_window
    clock[0] += 60
    request(send, model="m", payload={"format": "json"})
    assert len(calls) == 2 * per_window
    (fail if mode == "advisory" else passing)(store)
    request(send, model="m", payload={"format": "json"})
    assert len(calls) == 3 * per_window
    context[0] = 4096
    request(send, model="m", payload={"format": "json"})
    assert len(calls) == 4 * per_window


@pytest.mark.parametrize("mode", ["advisory", "strict"])
@pytest.mark.parametrize("method", ["request_primary", "request"])
def test_pool_only_strict_reobserves_after_dispatch_spans_ttl(tmp_path, mode, method):
    from sonder_runtime.application.routing.identity_cache import IdentityObservationCache

    clock = [10.0]
    calls = []
    pool, store = pool_for(tmp_path, mode=mode)
    pool._identity_cache = IdentityObservationCache(clock=lambda: clock[0])
    pool._identity_for = lambda origin, *_: calls.append(origin) or identity()
    (fail if mode == "advisory" else passing)(store)

    def send(origin):
        clock[0] += 60
        return origin

    assert getattr(pool, method)(send, model="m", payload={"format": "json"}) == PRIMARY
    assert len(calls) == (1 if mode == "advisory" else 2)


@pytest.mark.parametrize("mode", ["advisory", "strict", "off"])
@pytest.mark.parametrize("payload", [None, {"prompt": "plain"}, {"format": {"type": "object"}}])
def test_request_primary_dispatches_to_caller_origin(tmp_path, mode, payload):
    """server._post passes its BASE; the gate must never retarget the dispatch."""
    caller = "http://localhost:11434"
    probed = []

    def identity_for(origin, *_):
        probed.append(origin)
        return identity()  # same backend reachable under both spellings

    pool, store = pool_for(tmp_path, mode=mode, identity_for=identity_for)
    passing(store)
    probed.clear()
    sent = []
    pool.request_primary(sent.append, model="m", payload=payload, origin=caller)
    pool.request_primary(sent.append, model="m", payload=payload)
    assert sent == [caller, PRIMARY]
    if mode == "strict" and payload and "format" in payload:
        assert caller in probed  # identity is observed where the request goes
    else:
        assert probed == []  # advisory/off and plain requests never probe
