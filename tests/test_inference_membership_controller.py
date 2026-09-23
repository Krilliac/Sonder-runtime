"""Offline static membership, runtime lifecycle, and pool admission contracts."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from urllib.error import URLError
from types import SimpleNamespace

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


def test_application_composition_reexports_the_exact_independent_graph_type():
    from sonder_runtime.bootstrap.app import Application
    from sonder_runtime.bootstrap.application_graph import Application as Graph

    assert Application is Graph


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


def test_metric_identity_reservations_survive_removal_replacement_and_new_pool():
    from sonder_runtime.platform.metrics import MetricsRegistry

    class Metrics(MetricsRegistry):
        def __init__(self):
            super().__init__(enabled=False)
            self.requests = []

        def observe_ollama_worker_request(self, *, worker, result, elapsed_seconds):
            self.requests.append((worker, result))

    metrics = Metrics()
    clock = Clock()
    source = Source(signed_snapshot(worker_prefix="first"))
    pool = OllamaWorkerPool(REMOTE, (REPLACEMENT,), allow_remote=True, metrics=metrics,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, clock)
    try:
        control.refresh(timeout_seconds=2)
        assert pool.request(lambda origin: origin) == REMOTE
        source.snapshot = signed_snapshot((), generation=2)
        control.refresh(timeout_seconds=2)
        assert pool.origins == ()
        source.snapshot = signed_snapshot(generation=3, worker_prefix="second")
        control.refresh(timeout_seconds=2)
        pool.request(lambda origin: origin)
        # Endpoint and incarnation changes keep the same admitted identity.
        source.snapshot = signed_snapshot((REPLACEMENT,), generation=4,
                                          member_generation=2, worker_prefix="second")
        control.refresh(timeout_seconds=2)
        assert pool.request(lambda origin: origin) == REPLACEMENT
        source.snapshot = signed_snapshot(generation=5, worker_prefix="first")
        control.refresh(timeout_seconds=2)
        pool.request(lambda origin: origin)
        assert metrics.requests == [("w0", "ok"), ("w1", "ok"), ("w1", "ok"), ("w0", "ok")]
    finally:
        assert control.close(timeout=2)

    # Re-composition shares the process metric owner, not recycled pool slots.
    source = Source(signed_snapshot(worker_prefix="third"))
    pool = OllamaWorkerPool(REMOTE, allow_remote=True, metrics=metrics,
                            capability_prober=lambda _: {"models": ["code"]})
    control = controller(source, pool, clock)
    try:
        control.refresh(timeout_seconds=2)
        pool.request(lambda origin: origin)
        assert metrics.requests[-1] == ("w2", "ok")
    finally:
        assert control.close(timeout=2)


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


def test_readding_origin_reuses_unresolved_probe_state():
    clock = Clock()
    source = Source(signed_snapshot())
    entered, release = threading.Event(), threading.Event()

    def stuck(_origin):
        entered.set()
        release.wait(5)
        return {"models": ["code"]}

    pool = OllamaWorkerPool(
        REMOTE,
        allow_remote=True,
        capability_prober=stuck,
        capability_probe_timeout_seconds=0.05,
    )
    control = controller(source, pool, clock)
    try:
        control.refresh(timeout_seconds=2)
        assert entered.wait(1)
        assert len(pool._states) == 1

        source.snapshot = signed_snapshot((), generation=2)
        control.refresh(timeout_seconds=2, probe=False)
        assert len(pool._states) == 1

        source.snapshot = signed_snapshot((REMOTE,), generation=3, member_generation=2)
        control.refresh(timeout_seconds=2, probe=False)
        assert len(pool._states) == 1
        assert pool.origins == (REMOTE,)
        assert pool._states[0].capability_probe_inflight is True
    finally:
        release.set()
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


@pytest.mark.parametrize("primary_remote", [False, True])
@pytest.mark.parametrize("command", ["serve", "mcp", "repl", "bound_direct"])
def test_entrypoint_legacy_requests_share_typed_membership_admission(
    monkeypatch, tmp_path, primary_remote, command,
):
    import server
    import sonder_runtime.__main__ as entrypoint
    from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
    from sonder_runtime.adapters.persistence import migrations, operations_store
    from sonder_runtime.adapters.persistence.sqlite import bridge_migration
    from sonder_runtime.bootstrap import app as bootstrap, legacy_root
    from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    config = SonderConfig(state=StateConfig(home=str(tmp_path)), ollama=OllamaConfig(
        url=REMOTE if primary_remote else LOCAL, workers=() if primary_remote else (REMOTE,),
        allow_remote=True, worker_capability_ttl_seconds=60))
    # Exercise the preloaded legacy case as well as the real command wiring:
    # this is the otherwise-uncontrolled pool that the serve command must retire.
    stale_pool = OllamaWorkerPool(config.ollama.url, config.ollama.workers, allow_remote=True,
        capability_prober=lambda _: {"models": ["remote-model"]})
    monkeypatch.setattr(server, "OLLAMA_POOL", stale_pool)
    monkeypatch.setattr(server, "BASE", config.ollama.url)
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    monkeypatch.setattr(bootstrap, "_application_lifecycle", ApplicationLifecycle(bootstrap._build_default_application))
    for name in ("_default_config", "_default_compute_close", "_default_delegation_close", "_default_inference_close"):
        monkeypatch.setattr(bootstrap, name, None)
    monkeypatch.setattr(entrypoint, "_load_config", lambda _: config)
    monkeypatch.setattr(entrypoint, "_export_runtime_environment", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bridge_migration, "require_epoch_2", lambda _: None)
    monkeypatch.setattr(migrations, "migrate_all", lambda **_: None)
    monkeypatch.setattr(operations_store, "OperationsStore", lambda: SimpleNamespace(prune_events=lambda _: 0))
    calls = []

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, *_):
            return b'{"ok":true}'

    def transport(request, **_):
        calls.append(request.full_url)
        return Response()

    # Keep the real consent/canonical HTTPS policy; replace only network I/O.
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    monkeypatch.setattr(ollama_endpoint._OPENER, "open", transport)
    monkeypatch.setattr(server, "dispatch_provider", lambda _provider, _path, _payload, send: send())
    monkeypatch.setattr(ollama_pool, "_default_capability_prober", lambda **_: (
        lambda origin: {"models": ["remote-model" if origin == REMOTE else "local-model"]}))
    checked = []

    def run_interface(**_):
        application = bootstrap.default_app()
        with pytest.raises(ollama_pool.WorkerPoolError):
            server._post("/api/generate", {"model": "remote-model"})
        assert calls == []
        assert application.config is config
        assert server.OLLAMA_POOL is application.inference_pool
        assert ollama_pool.from_environment(config.ollama.url) is application.inference_pool
        assert server._application() is application
        assert application.inference_membership._thread is None
        if primary_remote:
            with pytest.raises(ollama_pool.WorkerPoolError):
                server._get("/api/tags")
            with pytest.raises(ollama_pool.WorkerPoolError):
                server._post("/api/generate", {"model": "remote-model"}, local_only=True)
        assert stale_pool is not application.inference_pool
        with pytest.raises(ollama_pool.WorkerPoolDraining):
            stale_pool.request(lambda _: pytest.fail("retired legacy pool admitted"))
        clock = Clock()
        control = application.inference_membership
        control._clock = control._source._clock = application.inference_pool._membership_clock = clock
        control.refresh(timeout_seconds=2)
        assert server._post("/api/generate", {"model": "remote-model"}) == {"ok": True}
        assert calls == [REMOTE + "/api/generate"]
        clock.now += timedelta(seconds=61)
        with pytest.raises(ollama_pool.WorkerPoolError):
            server._post("/api/generate", {"model": "remote-model"})
        if primary_remote:
            with pytest.raises(ollama_pool.WorkerPoolError):
                server._get("/api/tags")
            with pytest.raises(ollama_pool.WorkerPoolError):
                server._post("/api/generate", {"model": "remote-model"}, local_only=True)
        assert calls == [REMOTE + "/api/generate"]
        checked.append(True)

    monkeypatch.setattr(serve, "main", run_interface)
    monkeypatch.setattr(server.mcp, "run", run_interface)
    monkeypatch.setattr(server, "require_mcp_startup_safety", lambda: None)
    monkeypatch.setattr(repl, "main", run_interface)
    try:
        args = SimpleNamespace(skip_preflight=True, native=False, json=False)
        if command == "bound_direct":
            application = bootstrap.default_app(config=config)
            legacy_root.configure_application(application)
            server.run_mcp()
        else:
            assert getattr(entrypoint, "cmd_" + command)(args) == 0
        assert checked == [True]
    finally:
        bootstrap.close_default_runtime_resources(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("configuration", ["remote_primary", "remote_worker", "local", "local_lab", "invalid_lab",
    "impostor_remote_primary", "impostor_remote_worker", "typed_local_config_remote_pool",
    "typed_remote_config_local_pool", "typed_source_impostor", "typed_remote_primary", "typed_remote_worker"])
def test_executable_root_refuses_unbound_remote_membership_in_isolated_process(tmp_path, configuration):
    root = Path(__file__).resolve().parents[1]
    transport_configuration = configuration.removeprefix("impostor_")
    if configuration.startswith("typed_"):
        transport_configuration = ("local" if configuration == "typed_remote_config_local_pool"
                                   else "remote_worker" if configuration == "typed_remote_worker" else "remote_primary")
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("SONDER_", "OLLAMA_"))}
    environment.update(SONDER_HOME=str(tmp_path), SONDER_DB=str(tmp_path / "memory.db"),
                       SONDER_FLEET_DB=str(tmp_path / "fleet.db"), SONDER_FLEET_HEARTBEAT="0",
                       SONDER_ALLOW_CLOUD="0", SONDER_WEB_TOOLS="0", SONDER_LIVE_RELOAD="0",
                       SONDER_EMBED_CACHE="0", SONDER_FALLBACK_LOCAL="0", SONDER_HOST="127.0.0.1",
                       OLLAMA_HOST=REMOTE if transport_configuration == "remote_primary" else LOCAL,
                       SONDER_OLLAMA_WORKERS=REMOTE if transport_configuration == "remote_worker" else "",
                       SONDER_ALLOW_REMOTE_OLLAMA="1" if transport_configuration.startswith("remote_") or configuration.startswith("typed_") else "0")
    # Run the executable branch in a fresh interpreter: no already-imported
    # server module, typed pool cache, or lab acknowledgement can hide its path.
    script = r'''
import inspect, json, os, runpy, socket, sys
from types import SimpleNamespace
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import reloadable_mcp
from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
from sonder_runtime.adapters.security import unsafe_lab

def deny_network(*args, **kwargs):
    raise AssertionError("unexpected real network")
socket.create_connection = deny_network
socket.socket.connect = deny_network
mode = sys.argv[1]
if mode in ("local_lab", "invalid_lab"):
    os.environ[unsafe_lab.ACK_ENV] = unsafe_lab.ACKNOWLEDGEMENT if mode == "local_lab" else "true"
    unsafe_lab.is_privileged = lambda: False
assert (unsafe_lab.ACK_ENV in os.environ) == (mode in ("local_lab", "invalid_lab"))
assert "server" not in sys.modules
assert ollama_pool._configured_pool is None
result = {"started": False, "calls": [], "error": None}
application = None
finish = reloadable_mcp.ReloadableMCPServer.finish_module_refresh
def finish_with_impostor(self, module_name, source_path, namespace=None):
    global application
    value = finish(self, module_name, source_path, namespace)
    if mode.startswith("impostor_"):
        pool = namespace["OLLAMA_POOL"]
        namespace["_APP_GRAPH"] = SimpleNamespace(config=object(), inference_pool=pool,
                                                  inference_membership=SimpleNamespace(_pool=pool))
    elif mode.startswith("typed_"):
        from sonder_runtime.bootstrap.app import build_application
        from sonder_runtime.platform.config import SonderConfig, OllamaConfig, StateConfig
        config = SonderConfig(state=StateConfig(home=os.environ["SONDER_HOME"]), ollama=OllamaConfig(
            url=os.environ["OLLAMA_HOST"],
            workers=tuple(filter(None, os.environ["SONDER_OLLAMA_WORKERS"].split(","))),
            allow_remote=True, worker_capability_ttl_seconds=60))
        application = build_application(config=config)
        if mode in ("typed_local_config_remote_pool", "typed_remote_config_local_pool"):
            other = "http://127.0.0.1:11434" if mode == "typed_local_config_remote_pool" else "https://worker.example:11434"
            application = replace(application, config=replace(config, ollama=replace(config.ollama, url=other)))
        elif mode == "typed_source_impostor":
            source = application.inference_membership._source
            application.inference_membership._source = SimpleNamespace(**source.__dict__)
        namespace["_APP_GRAPH"] = application
        namespace["OLLAMA_POOL"] = application.inference_pool
        namespace["BASE"] = application.config.ollama.url
        assert application.inference_membership._thread is None
        assert "server" not in sys.modules
    return value
reloadable_mcp.ReloadableMCPServer.finish_module_refresh = finish_with_impostor
class Response:
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, *_): return b'{"ok":true}'
def send(request, **kwargs):
    result["calls"].append(request.full_url)
    return Response()
ollama_endpoint._OPENER.open = send
ollama_pool._default_capability_prober = lambda **kwargs: lambda origin: {
    "models": ["local" if mode.startswith("typed_") and ollama_endpoint.is_loopback(origin) else "code"]}
def run(self):
    result["started"] = True
    namespace = inspect.currentframe().f_back.f_globals
    assert namespace["__name__"] == "__main__"
    assert (namespace["_APP_GRAPH"] is None) == (not mode.startswith(("impostor_", "typed_")))
    namespace["dispatch_provider"] = lambda _provider, _path, _payload, transport: transport()
    if mode in ("typed_remote_primary", "typed_remote_worker"):
        def refused():
            try:
                namespace["_post"]("/api/generate", {"model": "code"})
            except ollama_pool.WorkerPoolError:
                return
            raise AssertionError("remote work admitted without current evidence")
        refused()
        assert result["calls"] == []
        class Clock:
            now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)
            def __call__(self): return self.now
        clock = Clock()
        control = application.inference_membership
        control._clock = control._source._clock = application.inference_pool._membership_clock = clock
        control.refresh(timeout_seconds=2)
        namespace["_post"]("/api/generate", {"model": "code"})
        clock.now += timedelta(seconds=61)
        refused()
        return
    for _ in range(2):
        namespace["_post"]("/api/generate", {"model": "code"})
reloadable_mcp.ReloadableMCPServer.run = run
try:
    runpy.run_path("server.py", run_name="__main__")
except (ollama_pool.WorkerPoolError, unsafe_lab.UnsafeLabError) as error:
    result["error"] = type(error).__name__
finally:
    if application is not None:
        application.close_providers(timeout=2)
print("ROOT_RESULT=" + json.dumps(result, sort_keys=True))
'''
    completed = subprocess.run([sys.executable, "-c", script, configuration], cwd=root,
                               env=environment, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr[-3000:]
    result = json.loads(next(line.removeprefix("ROOT_RESULT=") for line in completed.stdout.splitlines()
                             if line.startswith("ROOT_RESULT=")))
    if configuration in ("typed_remote_primary", "typed_remote_worker"):
        assert result == {"started": True, "calls": [REMOTE + "/api/generate"], "error": None}
    elif transport_configuration.startswith("remote_") or configuration == "typed_remote_config_local_pool":
        assert result == {"started": False, "calls": [], "error": "WorkerPoolUnavailable"}
    elif configuration == "invalid_lab":
        assert result == {"started": False, "calls": [], "error": "UnsafeLabError"}
    else:
        assert result == {"started": True, "calls": [LOCAL + "/api/generate"] * 2, "error": None}


@pytest.mark.parametrize("seam", ["root", "interfaces", "mcp", "run_mcp"])
@pytest.mark.parametrize("local_configuration", [False, True])
@pytest.mark.parametrize("impostor", ["application_duck", "application_subclass", "controller_duck",
                                     "controller_subclass", "pool_duck", "pool_subclass",
                                     "raw_pool", "foreign_controller", "closed_controller",
                                     "outgoing_pool", "outgoing_application", "outgoing_controller"])
def test_legacy_membership_binding_rejects_impostors_before_adapter_or_pool_calls(
    monkeypatch, tmp_path, seam, impostor, local_configuration,
):
    import server
    from sonder_runtime.bootstrap import app as bootstrap, legacy_interfaces, legacy_mcp, legacy_root
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    origin = LOCAL if local_configuration else REMOTE
    application = bootstrap.build_application(config=SonderConfig(
        state=StateConfig(home=str(tmp_path)), ollama=OllamaConfig(url=origin, allow_remote=True)))
    pool, control = application.inference_pool, application.inference_membership
    proposed = application

    def subclass(value):
        kind = type("HostileSubclass", (type(value),), {"__eq__": lambda *_: True})
        result = object.__new__(kind)
        result.__dict__.update(value.__dict__)
        return result

    if impostor == "application_duck":
        proposed = SimpleNamespace(**application.__dict__)
    elif impostor == "application_subclass":
        proposed = subclass(application)
    elif impostor == "controller_duck":
        proposed = replace(application, inference_membership=SimpleNamespace(_pool=pool))
    elif impostor == "controller_subclass":
        proposed = replace(application, inference_membership=subclass(control))
    elif impostor in ("pool_duck", "pool_subclass", "raw_pool"):
        replacement = (SimpleNamespace(**pool.__dict__, has_remote_workers=False) if impostor == "pool_duck"
                       else subclass(pool) if impostor == "pool_subclass"
                       else OllamaWorkerPool(origin, allow_remote=True))
        control._pool = replacement
        proposed = replace(application, inference_pool=replacement)
    elif impostor == "foreign_controller":
        control._pool = OllamaWorkerPool(REMOTE, allow_remote=True)
    elif impostor == "closed_controller":
        control.close(timeout=0)

    calls = []
    old_pool = OllamaWorkerPool(LOCAL)
    monkeypatch.setattr(old_pool, "drain", lambda **_: calls.append("drain"))
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "OLLAMA_POOL", old_pool)
    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("adapter"))
    if impostor == "outgoing_pool":
        old_pool = SimpleNamespace(drain=lambda **_: calls.append("impostor drain"))
        monkeypatch.setattr(server, "OLLAMA_POOL", old_pool)
    elif impostor in ("outgoing_application", "outgoing_controller"):
        if impostor == "outgoing_controller":
            previous = replace(application, inference_membership=SimpleNamespace(
                _pool=pool, close=lambda **_: calls.append("impostor close")))
            old_pool = pool
            monkeypatch.setattr(server, "OLLAMA_POOL", old_pool)
        else:
            previous = SimpleNamespace(close_providers=lambda **_: calls.append("impostor close"))
        monkeypatch.setattr(server, "_APP_GRAPH", previous)
        monkeypatch.setattr(legacy_root, "_owned_application", previous)
    try:
        if seam == "run_mcp":
            # A fake pool cannot claim locality to avoid the exact-type check.
            monkeypatch.setattr(server, "_APP_GRAPH", previous if impostor in (
                "outgoing_application", "outgoing_controller") else proposed)
            monkeypatch.setattr(server, "OLLAMA_POOL", old_pool if impostor == "outgoing_pool" else proposed.inference_pool)
            with pytest.raises(ollama_pool.WorkerPoolUnavailable):
                server.run_mcp(safety_checked=True)
        else:
            bind = {"root": legacy_root.configure_application,
                    "interfaces": legacy_interfaces.configure_legacy_application,
                    "mcp": legacy_mcp.configure_legacy_application}[seam]
            with pytest.raises(ValueError, match="membership binding"):
                bind(proposed)
            assert server._APP_GRAPH is (previous if impostor in (
                "outgoing_application", "outgoing_controller") else None)
            assert server.OLLAMA_POOL is old_pool
        assert calls == []
    finally:
        control._pool = pool
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


def test_owned_default_binding_accepts_genuine_typed_local_application(monkeypatch, tmp_path):
    from sonder_runtime.bootstrap import app as bootstrap
    from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = bootstrap.build_application(config=SonderConfig(state=StateConfig(home=str(tmp_path))))
    monkeypatch.setattr(bootstrap, "_application_lifecycle", ApplicationLifecycle(
        lambda: pytest.fail("owned application fell back to factory")))
    for name in ("_owned_default_application", "_default_config", "_default_compute_close",
                 "_default_delegation_close", "_default_inference_close"):
        monkeypatch.setattr(bootstrap, name, None)
    try:
        bootstrap.install_owned_application(application)
        assert bootstrap.default_app(config=application.config) is application
        assert application.inference_membership._thread is None
        bootstrap.stop_owned_application(application)
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("mismatch", ["pool", "primary"])
def test_direct_mcp_requires_exact_application_pool_and_primary_link(monkeypatch, tmp_path, mismatch):
    import server
    from sonder_runtime.bootstrap import app as bootstrap
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = bootstrap.build_application(config=SonderConfig(state=StateConfig(home=str(tmp_path))))
    monkeypatch.setattr(server, "_APP_GRAPH", application)
    monkeypatch.setattr(server, "BASE", REMOTE if mismatch == "primary" else LOCAL)
    monkeypatch.setattr(server, "OLLAMA_POOL", OllamaWorkerPool(LOCAL) if mismatch == "pool" else application.inference_pool)
    monkeypatch.setattr(server.mcp, "run", lambda: pytest.fail("mismatched binding reached adapter"))
    try:
        with pytest.raises(ollama_pool.WorkerPoolUnavailable):
            server.run_mcp(safety_checked=True)
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("impostor", ["controller_duck", "pool_duck", "raw_pool"])
def test_owned_default_binding_rejects_structural_inference_before_install(monkeypatch, tmp_path, impostor):
    from sonder_runtime.bootstrap import app as bootstrap
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = bootstrap.build_application(config=SonderConfig(
        state=StateConfig(home=str(tmp_path)), ollama=OllamaConfig(url=REMOTE, allow_remote=True)))
    pool, control = application.inference_pool, application.inference_membership
    if impostor == "controller_duck":
        proposed = replace(application, inference_membership=SimpleNamespace(_pool=pool))
    else:
        replacement = (SimpleNamespace(**pool.__dict__) if impostor == "pool_duck"
                       else OllamaWorkerPool(REMOTE, allow_remote=True))
        control._pool = replacement
        proposed = replace(application, inference_pool=replacement)
    calls = []
    monkeypatch.setattr(bootstrap._application_lifecycle, "install_owned", lambda _: calls.append("installed"))
    for name in ("_owned_default_application", "_default_config", "_default_compute_close",
                 "_default_delegation_close", "_default_inference_close"):
        monkeypatch.setattr(bootstrap, name, None)
    try:
        with pytest.raises(ValueError, match="membership binding"):
            bootstrap.install_owned_application(proposed)
        assert calls == [] and bootstrap._owned_default_application is None
    finally:
        control._pool = pool
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("seam", ["root", "interfaces", "mcp", "run_mcp", "owned", "default", "owned_default"])
@pytest.mark.parametrize("mismatch", ["local_config_remote_pool", "remote_config_local_pool",
    "source_duck", "source_subclass", "source_origins", "source_clock", "source_authority",
    "source_ttl", "source_capacity", "worker_config", "origin_equality"])
def test_binding_requires_configured_origins_and_exact_static_source(monkeypatch, tmp_path, seam, mismatch):
    import server
    from sonder_runtime.bootstrap import app as bootstrap, legacy_interfaces, legacy_mcp, legacy_root
    from sonder_runtime.adapters.application_lifecycle import ApplicationLifecycle
    from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
    from sonder_runtime.adapters.inference.static_membership import StaticMembershipSource
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    origin = LOCAL if mismatch == "remote_config_local_pool" else REMOTE
    application = bootstrap.build_application(config=SonderConfig(
        state=StateConfig(home=str(tmp_path)), ollama=OllamaConfig(url=origin, allow_remote=True)))
    pool, control = application.inference_pool, application.inference_membership
    proposed = application
    if mismatch.endswith("_pool"):
        proposed = replace(application, config=replace(application.config, ollama=OllamaConfig(
            url=REMOTE if origin == LOCAL else LOCAL, allow_remote=True)))
    elif mismatch == "worker_config":
        proposed = replace(application, config=replace(application.config,
            ollama=replace(application.config.ollama, workers=(LOCAL,))))
    elif mismatch == "source_duck":
        control._source = SimpleNamespace(**control._source.__dict__)
    elif mismatch == "source_subclass":
        kind = type("HostileSource", (StaticMembershipSource,), {"__eq__": lambda *_: True})
        source = object.__new__(kind)
        source.__dict__.update(control._source.__dict__)
        control._source = source
    elif mismatch == "source_origins":
        control._source = StaticMembershipSource(OllamaConfig(url=LOCAL), clock=control._clock)
    elif mismatch in ("source_ttl", "source_capacity"):
        field = "worker_capability_ttl_seconds" if mismatch == "source_ttl" else "worker_max_inflight"
        control._source = StaticMembershipSource(replace(application.config.ollama,
            **{field: getattr(application.config.ollama, field) + 1}), clock=control._clock)
    elif mismatch == "source_clock":
        control._source._clock = Clock()
    elif mismatch == "source_authority":
        control._source.issuer_id = "other-issuer"
    else:
        class HostileOrigin(str):
            def __eq__(self, other):
                pytest.fail("binding invoked hostile equality")
        pool._configured_origins = (HostileOrigin(LOCAL),)

    calls = []
    monkeypatch.setattr(ollama_endpoint._OPENER, "open", lambda *_a, **_k: calls.append("network"))
    monkeypatch.setattr(server.mcp, "run", lambda: calls.append("adapter"))
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "BASE", proposed.config.ollama.url)
    monkeypatch.setattr(server, "_APP_GRAPH", proposed if seam == "run_mcp" else None)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    monkeypatch.setattr(bootstrap, "_application_lifecycle", ApplicationLifecycle(lambda: proposed))
    for name in ("_owned_default_application", "_default_compute_close", "_default_delegation_close",
                 "_default_inference_close"):
        monkeypatch.setattr(bootstrap, name, proposed if name == "_owned_default_application" and seam == "owned_default" else None)
    monkeypatch.setattr(bootstrap, "_default_config", proposed.config)
    try:
        with pytest.raises(WorkerPoolUnavailable if seam == "run_mcp" else ValueError):
            if seam == "run_mcp":
                server.run_mcp(safety_checked=True)
            elif seam == "owned":
                bootstrap.install_owned_application(proposed)
            elif seam in ("default", "owned_default"):
                bootstrap.default_app(config=proposed.config)
            else:
                {"root": legacy_root.configure_application,
                 "interfaces": legacy_interfaces.configure_legacy_application,
                 "mcp": legacy_mcp.configure_legacy_application}[seam](proposed)
        assert calls == []
        assert control._thread is None
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


def test_configured_origin_binding_survives_expired_empty_live_roster(monkeypatch, tmp_path):
    from sonder_runtime.bootstrap import app as bootstrap, legacy_root
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    application = bootstrap.build_application(config=SonderConfig(state=StateConfig(home=str(tmp_path)),
        ollama=OllamaConfig(url="https://WORKER.example:11434/", allow_remote=True,
                            worker_capability_ttl_seconds=60)))
    pool, control = application.inference_pool, application.inference_membership
    clock = Clock()
    control._clock = control._source._clock = pool._membership_clock = clock
    pool._capability_prober = lambda _: {"models": ["code"]}
    try:
        assert pool.configured_origins == control._source.configured_origins == (REMOTE,)
        with pytest.raises(AttributeError):
            pool.configured_origins = (LOCAL,)
        with pytest.raises(AttributeError):
            control._source.configured_origins = (LOCAL,)
        assert legacy_root.require_inference_application(application) is pool
        control.refresh(timeout_seconds=2)
        clock.now += timedelta(seconds=61)
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("expired member dispatched"), model="code")
        def outage(**_):
            raise TimeoutError("static source unavailable")
        monkeypatch.setattr(control._source, "read_snapshot", outage)
        assert control.refresh(timeout_seconds=2, probe=False).roster.members[0].lifecycle_state == "expired"
        pool.stop_membership()
        assert pool.origins == ()
        assert pool.configured_origins == (REMOTE,)
        assert legacy_root.require_inference_application(application) is pool
        legacy_root.require_mcp_inference_binding(application, pool, primary_origin=REMOTE)
        ollama_pool.configure_typed_pool(pool)
        assert ollama_pool.from_environment(REMOTE) is pool
    finally:
        application.close_providers(timeout=2)
        ollama_pool.reset_typed_workers()


@pytest.mark.parametrize("primary", [LOCAL, REMOTE])
def test_typed_pool_cache_rejects_primary_config_mismatch(monkeypatch, primary):
    from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool

    ollama_pool.configure_typed_workers((), allow_remote=True)
    monkeypatch.setattr(ollama_endpoint, "_configured_endpoint", primary)
    try:
        with pytest.raises(ValueError):
            ollama_pool.configure_typed_pool(OllamaWorkerPool(
                REMOTE if primary == LOCAL else LOCAL, allow_remote=True))
        assert ollama_pool._configured_pool is None
    finally:
        ollama_pool.reset_typed_workers()
