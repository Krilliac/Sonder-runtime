"""Offline static membership, runtime lifecycle, and pool admission contracts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib
import json
import threading
from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool, WorkerPoolUnavailable
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.ports.inference_membership import MembershipSourceLimits
from sonder_runtime.domain.inference_membership import MembershipSnapshot
from sonder_runtime.platform.config import OllamaConfig

NOW = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
REMOTE = "https://worker.example:11434"
REPLACEMENT = "https://replacement.example:11434"
LOCAL = "http://127.0.0.1:11434"


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def signed_snapshot(origins=(REMOTE,), *, generation=1, member_generation=1, issued_at=NOW,
                    worker_prefix="worker"):
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    payload = dict(cluster_id="static-config", issuer_id="local-config", generation=generation,
                   protocol_version=1, issued_at=issued_at.isoformat(),
                   expires_at=(issued_at + timedelta(seconds=60)).isoformat(),
                   workers=[dict(worker_id="%s-%d" % (worker_prefix, i), origin=origin,
                                 member_generation=member_generation, lifecycle_state="active",
                                 models=[], advertised_capacity=1) for i, origin in enumerate(origins)])
    key = b"offline-controller-fixture"
    signature = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()
    raw = canonical(dict(payload=payload, signature=signature))

    def verify(value):
        parsed = json.loads(value)
        expected = hmac.new(key, canonical(parsed["payload"]), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, parsed["signature"])
    return MembershipSnapshot.from_signed_envelope(raw, verify=verify)


class Source:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0
        self.check = lambda: None

    def read_snapshot(self, *, limits):
        self.calls += 1
        self.check()
        if isinstance(self.snapshot, Exception):
            raise self.snapshot
        return self.snapshot


def controller(source, pool, clock):
    module = importlib.import_module("sonder_runtime.application.inference_membership.controller")
    return module.MembershipController(source, pool, clock=clock, cluster_id="static-config",
                                       issuer_id="local-config", refresh_interval_seconds=60)


def test_snapshot_sequence_probation_activation_removal_and_expiry():
    clock = Clock()
    source = Source(signed_snapshot())
    probes = []
    pool = OllamaWorkerPool(REMOTE, allow_remote=True,
                            capability_prober=lambda origin: probes.append(origin) or {"models": ["code"]})
    control = controller(source, pool, clock)
    try:
        assert source.calls == 0
        assert pool.summary()["eligible_worker_count"] == 0
        assert probes == []
        result = control.refresh(timeout_seconds=2, probe=False)
        assert result.roster.members[0].lifecycle_state == "probation"
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("probation admitted"))
        result = control.refresh(timeout_seconds=2)
        assert result.roster.members[0].lifecycle_state == "active"
        assert probes == [REMOTE]
        assert pool.request(lambda origin: origin) == REMOTE
        source.snapshot = OSError("offline")
        clock.now += timedelta(seconds=61)
        # Expiry must block admission even if the controller has not refreshed.
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("expired member admitted"))
        assert control.refresh(timeout_seconds=2).roster.members[0].lifecycle_state == "expired"
    finally:
        assert control.close(timeout=2)


@pytest.mark.parametrize("replace_endpoint", [False, True])
def test_removal_or_replacement_drains_original_inflight_endpoint(replace_endpoint):
    clock = Clock()
    source = Source(signed_snapshot())
    pool = OllamaWorkerPool(REMOTE, (REPLACEMENT,), allow_remote=True,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, clock)
    entered, release = threading.Event(), threading.Event()
    sent, results = [], []

    def sender(origin):
        sent.append(origin)
        entered.set()
        assert release.wait(3)
        return origin

    thread = threading.Thread(target=lambda: results.append(pool.request(sender)))
    try:
        control.refresh(timeout_seconds=2)
        thread.start()
        assert entered.wait(2)
        source.snapshot = signed_snapshot((REPLACEMENT,) if replace_endpoint else (),
                                          generation=2, member_generation=2)
        control.refresh(timeout_seconds=2, probe=False)
        assert any(row.origin == REMOTE and row.state == "draining" for row in pool.snapshots())
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("new member admitted without probe"))
        if replace_endpoint:
            control.refresh(timeout_seconds=2)
            assert pool.request(lambda origin: origin) == REPLACEMENT
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert sent == results == [REMOTE]
        assert REMOTE not in pool.origins
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(2)
        assert control.close(timeout=2)


def test_reconciliation_never_replays_a_response_bearing_failure():
    source = Source(signed_snapshot((REMOTE, REPLACEMENT)))
    pool = OllamaWorkerPool(REMOTE, (REPLACEMENT,), allow_remote=True,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, Clock())
    calls = []
    try:
        control.refresh(timeout_seconds=2)

        def sender(origin):
            calls.append(origin)
            source.snapshot = signed_snapshot((), generation=2)
            control.refresh(timeout_seconds=2)
            raise ModelCallError("protocol", "response envelope rejected")

        with pytest.raises(ModelCallError):
            pool.request(sender, idempotent=True)
        assert len(calls) == 1
    finally:
        assert control.close(timeout=2)


def test_source_and_probes_run_outside_pool_condition_and_status_is_cached():
    source = Source(signed_snapshot())
    pool = OllamaWorkerPool(REMOTE, allow_remote=True)

    def unlocked():
        acquired = []
        def inspect():
            with pool._condition:
                acquired.append(True)
        thread = threading.Thread(target=inspect)
        thread.start()
        thread.join(1)
        assert acquired == [True]
    source.check = unlocked
    pool._capability_prober = lambda _: unlocked() or {"models": ["code"]}
    control = controller(source, pool, Clock())
    try:
        control.refresh(timeout_seconds=2)
        calls = source.calls
        pool._capability_prober = lambda _: pytest.fail("cached projection probed")
        pool.summary()
        pool.status(page_size=1)
        assert source.calls == calls
    finally:
        assert control.close(timeout=2)


def test_existing_explicit_capability_refresh_probes_only_admitted_remote_origins():
    source = Source(signed_snapshot())
    calls = []
    pool = OllamaWorkerPool(REMOTE, (REPLACEMENT,), allow_remote=True,
                            capability_prober=lambda origin: calls.append(origin) or {"models": ["code"]})
    control = controller(source, pool, Clock())
    try:
        pool.refresh_capabilities(force=True)
        assert calls == []
        control.refresh(timeout_seconds=2, probe=False)
        pool.refresh_capabilities(force=True)
        assert calls == [REMOTE]
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("probe bypassed roster activation"))
        control.refresh(timeout_seconds=2)
        assert pool.request(lambda origin: origin) == REMOTE
    finally:
        assert control.close(timeout=2)


def test_static_source_keeps_loopback_lane_separate_and_bounds_signed_snapshot():
    module = importlib.import_module("sonder_runtime.adapters.inference.static_membership")
    config = OllamaConfig(workers=("https://localhost:11435", REMOTE), allow_remote=True)
    source = module.StaticMembershipSource(config, clock=Clock())
    first = source.read_snapshot(limits=MembershipSourceLimits())
    assert source.local_origins == (LOCAL, "https://127.0.0.1:11435")
    assert tuple(worker.origin for worker in first.workers) == (REMOTE,)
    assert first.digest == hashlib.sha256(first.canonical_envelope).hexdigest()
    assert source.read_snapshot(limits=MembershipSourceLimits()).generation > first.generation
    with pytest.raises(ValueError):
        source.read_snapshot(limits=MembershipSourceLimits(max_bytes=10))


@pytest.mark.parametrize("origin", ["http://worker.example:11434", "https://evil.example:11434"])
def test_membership_cannot_admit_unconfigured_or_cross_lane_origin(origin):
    source = Source(signed_snapshot())
    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, Clock())
    try:
        control.refresh(timeout_seconds=2)
        if origin.startswith("http:"):
            with pytest.raises(ValueError):
                signed_snapshot((origin,))
        else:
            source.snapshot = signed_snapshot((origin,), generation=2)
            control.refresh(timeout_seconds=2)
            assert origin not in pool.origins
        assert control.close(timeout=2)
        assert pool.request(lambda selected: selected) == LOCAL
    finally:
        assert control.close(timeout=2)


def test_refresh_and_close_timeout_retain_one_blocked_runtime_thread():
    source = Source(signed_snapshot())
    entered, release = threading.Event(), threading.Event()
    source.check = lambda: (entered.set(), release.wait(3))
    pool = OllamaWorkerPool(REMOTE, allow_remote=True)
    control = controller(source, pool, Clock())
    try:
        with pytest.raises(TimeoutError):
            control.refresh(timeout_seconds=.02)
        assert entered.is_set()
        with pytest.raises(RuntimeError, match="already running"):
            control.refresh(timeout_seconds=.02)
        assert not control.close(timeout=.02)
        with pytest.raises(RuntimeError):
            control.start()
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("closed controller admitted remote"))
    finally:
        release.set()
        assert control.close(timeout=2)
    assert source.calls == 1


def test_single_remote_pool_remains_enabled_after_its_membership_is_removed():
    source = Source(signed_snapshot())
    pool = OllamaWorkerPool(REMOTE, allow_remote=True)
    control = controller(source, pool, Clock())
    try:
        assert pool.enabled
        source.snapshot = signed_snapshot((), generation=2)
        control.refresh(timeout_seconds=2, probe=False)
        assert pool.origins == ()
        assert pool.enabled  # Compatibility callers must never bypass admission.
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("removed primary admitted"))
    finally:
        assert control.close(timeout=2)


def test_application_composes_shared_static_pool_without_starting_io(monkeypatch):
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig
    from sonder_runtime.adapters.inference import ollama_pool
    module = importlib.import_module("sonder_runtime.adapters.inference.static_membership")
    monkeypatch.setattr(module.StaticMembershipSource, "read_snapshot",
                        lambda *_args, **_kwargs: pytest.fail("composition read source"))
    monkeypatch.setattr(ollama_pool, "_default_capability_prober",
                        lambda **_: lambda _: pytest.fail("composition contacted worker"))
    app = build_application(config=SonderConfig(ollama=OllamaConfig(workers=(REMOTE,), allow_remote=True)))
    try:
        assert app.inference_membership is not None
        assert app.inference_pool is ollama_pool.from_environment(LOCAL)
        assert app.inference_pool.summary()["eligible_worker_count"] == 0
        assert app.inference_pool.request(lambda origin: origin) == LOCAL
    finally:
        app.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


def test_renewal_after_membership_expiry_requires_a_new_probe():
    clock = Clock()
    source = Source(signed_snapshot())
    probes = []
    pool = OllamaWorkerPool(REMOTE, allow_remote=True, capability_ttl_seconds=300,
                            capability_prober=lambda origin: probes.append(origin) or {"models": ["code"]})
    control = controller(source, pool, clock)
    try:
        control.refresh(timeout_seconds=2)
        assert probes == [REMOTE]
        clock.now += timedelta(seconds=61)
        source.snapshot = signed_snapshot(generation=2, issued_at=clock.now)
        control.refresh(timeout_seconds=2, probe=False)
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("renewed without attestation"))
        control.refresh(timeout_seconds=2)
        assert probes == [REMOTE, REMOTE]
    finally:
        assert control.close(timeout=2)


def test_renewed_membership_never_extends_cached_capability_evidence():
    clock = Clock()
    source = Source(signed_snapshot())
    pool = OllamaWorkerPool(REMOTE, allow_remote=True, capability_ttl_seconds=300,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, clock)
    try:
        first = control.refresh(timeout_seconds=2)
        clock.now += timedelta(seconds=50)
        source.snapshot = signed_snapshot(generation=2, issued_at=clock.now)
        renewed = control.refresh(timeout_seconds=2, probe=False)
        assert renewed.roster.members[0].evidence is first.roster.members[0].evidence
        clock.now += timedelta(seconds=11)
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("expired proof admitted under renewed lease"))
    finally:
        assert control.close(timeout=2)


@pytest.mark.parametrize("bad", [None, {}, "rollback", "digest-conflict", "loopback"])
def test_invalid_source_retains_only_previous_unexpired_remote_authority(bad):
    clock = Clock()
    source = Source(signed_snapshot(generation=2))
    pool = OllamaWorkerPool(REMOTE, ("https://127.0.0.1:11435",), allow_remote=True,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, clock)
    try:
        current = control.refresh(timeout_seconds=2)
        source.snapshot = (signed_snapshot() if bad == "rollback" else
                           signed_snapshot((), generation=2) if bad == "digest-conflict" else
                           signed_snapshot(("https://127.0.0.1:11435",), generation=3) if bad == "loopback" else bad)
        result = control.refresh(timeout_seconds=2)
        assert result.high_water is current.high_water
        assert result.roster.members[0].advertisement.origin == REMOTE
        assert result.roster.members[0].lifecycle_state == "active"
        clock.now += timedelta(seconds=61)
        assert control.refresh(timeout_seconds=2).roster.members[0].lifecycle_state == "expired"
    finally:
        assert control.close(timeout=2)


def test_repeated_replacements_never_exceed_state_limit_while_old_work_drains():
    source = Source(signed_snapshot())
    pool = OllamaWorkerPool(REMOTE, allow_remote=True, max_workers=1,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, Clock())
    entered, release = threading.Event(), threading.Event()
    thread = threading.Thread(target=lambda: pool.request(lambda _: (entered.set(), release.wait(4))))
    try:
        control.refresh(timeout_seconds=2)
        thread.start()
        assert entered.wait(2)
        for generation in range(2, 12):
            source.snapshot = signed_snapshot(generation=generation, member_generation=generation)
            control.refresh(timeout_seconds=2, probe=False)
            status = pool.summary()
            assert status["worker_count"] == status["draining_worker_count"] == 1
            assert status["membership_omitted_worker_count"] == 1
        release.set()
        thread.join(2)
        assert pool.origins == ()
        control.refresh(timeout_seconds=2)
        assert pool.request(lambda origin: origin) == REMOTE
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(2)
        assert control.close(timeout=2)


@pytest.mark.parametrize("changes", [
    {"allow_remote": False}, {"allow_remote": "yes"},
    {"workers": ("http://worker.example:11434",)},
    {"workers": ("https://WORKER.example:11434", REMOTE)},
])
def test_static_source_requires_exact_consented_configured_origins(changes):
    module = importlib.import_module("sonder_runtime.adapters.inference.static_membership")
    config = replace(OllamaConfig(workers=(REMOTE,), allow_remote=True), **changes)
    with pytest.raises(ValueError):
        module.StaticMembershipSource(config, clock=Clock())


def test_membership_churn_cannot_expand_a_logical_request_retry_budget():
    source = Source(signed_snapshot())
    pool = OllamaWorkerPool(REMOTE, allow_remote=True, max_workers=2,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, Clock())
    calls = []
    try:
        control.refresh(timeout_seconds=2)

        def sender(origin):
            calls.append(origin)
            if len(calls) > 2:
                pytest.fail("membership churn expanded retry budget")
            source.snapshot = signed_snapshot(generation=len(calls) + 1,
                                              worker_prefix="replacement-%d" % len(calls))
            control.refresh(timeout_seconds=2)
            raise URLError("connection did not produce a response")

        with pytest.raises(URLError):
            pool.request(sender, idempotent=True)
        assert len(calls) == 2
    finally:
        assert control.close(timeout=2)


def test_explicit_start_owns_one_periodic_thread_and_close_stops_it():
    source = Source(signed_snapshot())
    entered = threading.Event()
    source.check = entered.set
    pool = OllamaWorkerPool(REMOTE, allow_remote=True)
    module = importlib.import_module("sonder_runtime.application.inference_membership.controller")
    control = module.MembershipController(source, pool, clock=Clock(), cluster_id="static-config",
                                          issuer_id="local-config", refresh_interval_seconds=1)
    try:
        control.start()
        thread = control._thread
        control.start()
        assert control._thread is thread
        assert entered.wait(2)
    finally:
        assert control.close(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize("invalid_clock", [lambda: datetime(2026, 9, 7), lambda: 123])
def test_controller_rejects_non_aware_clock_before_changing_pool(invalid_clock):
    pool = OllamaWorkerPool(REMOTE, allow_remote=True)
    with pytest.raises(ValueError):
        controller(Source(signed_snapshot()), pool, invalid_clock)
    assert pool._membership_clock is None


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan"), True])
def test_refresh_rejects_invalid_timeout_before_starting_a_thread(timeout):
    pool = OllamaWorkerPool(REMOTE, allow_remote=True)
    source = Source(signed_snapshot())
    control = controller(source, pool, Clock())
    try:
        with pytest.raises(ValueError):
            control.refresh(timeout_seconds=timeout)
        assert control._thread is None and source.calls == 0
    finally:
        assert control.close(timeout=0)
