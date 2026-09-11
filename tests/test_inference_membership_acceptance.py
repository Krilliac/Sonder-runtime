"""Synthetic acceptance, not a deployed-cluster or throughput measurement.

All origins, signed envelopes, clocks, probes and senders are local fixtures.
The scheduler reservation seam saturates 256 workers without 256 host threads.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import socket
import threading
import time

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool, WorkerCapabilityUnavailable, WorkerPoolBackpressure,
    WorkerPoolUnavailable,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.inference_membership.controller import MembershipController
from sonder_runtime.domain.inference_membership import MembershipSnapshot
from sonder_runtime.domain.operational_capabilities import build_operational_capabilities
from sonder_runtime.platform.metrics import MetricsRegistry


SIZES = (16, 64, 256)


class Clock:
    now = datetime(2026, 9, 7, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def origins(count, *, local=False):
    return tuple(("http://127.0.0.1:%d" % (12000 + i)) if local else
                 ("https://member-%03d.example:11434" % i) for i in range(count))


def snapshot(endpoints, clock, *, generation=1, prefix="member", revoked=()):
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    payload = dict(cluster_id="acceptance", issuer_id="fixture", generation=generation,
                   protocol_version=1, issued_at=clock().isoformat(),
                   expires_at=(clock() + timedelta(seconds=60)).isoformat(),
                   workers=[dict(worker_id=prefix + "-" + origin.split("member-")[1].split(".")[0], origin=origin,
                                 member_generation=1,
                                 lifecycle_state="revoked" if origin in revoked else "active",
                                 models=[], advertised_capacity=64)
                            for i, origin in enumerate(endpoints)])
    key = b"synthetic-acceptance-fixture"
    signature = hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()

    def verify(raw):
        envelope = json.loads(raw)
        return hmac.compare_digest(envelope["signature"], hmac.new(
            key, canonical(envelope["payload"]), hashlib.sha256).hexdigest())

    return MembershipSnapshot.from_signed_envelope(
        canonical(dict(payload=payload, signature=signature)), verify=verify)


class Source:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def read_snapshot(self, *, limits):
        self.calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        assert len(self.value.workers) <= limits.max_advertisements
        assert len(self.value.canonical_envelope) <= limits.max_bytes
        return self.value


@contextmanager
def cluster(count, *, prober=None, batch=128, parallelism=2, metrics=None):
    endpoints, clock = origins(count), Clock()
    source = Source(snapshot(endpoints, clock))
    pool = OllamaWorkerPool(endpoints[0], endpoints[1:], max_workers=count,
                            allow_remote=True, max_inflight_per_worker=2, queue_depth=1,
                            capability_probe_batch_size=batch,
                            capability_probe_parallelism=parallelism, metrics=metrics,
                            capability_prober=prober or (lambda _: {"models": ["code"], "max_inflight": 1}))
    control = MembershipController(source, pool, clock=clock, cluster_id="acceptance",
                                   issuer_id="fixture", refresh_interval_seconds=86400)
    try:
        yield pool, control, source, clock, endpoints
    finally:
        assert control.close(timeout=5)


def activate(pool, control, count):
    for _ in range(count):
        control.refresh(timeout_seconds=5)
        if pool.summary()["eligible_worker_count"] == count:
            return
    pytest.fail("bounded refresh rotation did not activate the roster")


def wait_until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(.001)
    pytest.fail("synchronized test action did not complete")


def pages(pool, *, page_size):
    records, cursor = [], ""
    for _ in range(256):
        page = pool.status(page_size=page_size, cursor=cursor)
        assert len(page["workers"]) <= page_size
        assert len(json.dumps(page).encode("utf-8")) == page["serialized_bytes"] <= 65536
        records.extend(page["workers"])
        assert page["omitted_worker_count"] == page["worker_count"] - len(records)
        if page["complete"]:
            assert not page["next_cursor"]
            return records
        assert page["workers"] and page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    pytest.fail("paging did not terminate within the configured roster ceiling")


@pytest.mark.parametrize("count", SIZES)
def test_membership_batches_pages_and_capacity(count):
    calls = []
    models = tuple("model-%04d-%s" % (i, "x" * 180) for i in range(2048))
    def probe(origin):
        calls.append(origin)
        return {"models": models, "max_inflight": 64}

    with cluster(count, prober=probe, batch=5) as (pool, control, source, _, endpoints):
        control.refresh(timeout_seconds=5, probe=False)
        assert not calls and pool.summary()["available_capacity"] == 0
        for expected in range(5, count + 5, 5):
            before = len(calls)
            control.refresh(timeout_seconds=5)
            assert len(calls) - before <= 5
            assert pool.summary()["eligible_worker_count"] == min(expected, count)
        assert len(calls) == len(set(calls)) == count
        summary = pool.summary()
        assert summary["configured_worker_limit"] == count
        assert summary["available_capacity"] == 2 * count  # advertisement cannot raise local cap
        assert summary["queue"] == {"waiting": 0, "limit": 1, "scope": "global"}
        surface = build_operational_capabilities(config=None, inference_pool_status=summary)
        assert surface["inference"]["request_level_pooling"]["available"] is True
        assert surface["inference"]["model_sharding"]["available"] is False
        assert surface["compute"]["indefinite_scale"]["available"] is False
        reads = source.calls
        for size in (1, 128):
            records = pages(pool, page_size=size)
            assert len(records) == count
            assert {row["origin"] for row in records} == set(endpoints)
            for row in records:
                assert row["model_count"] == 2048 and len(row["model_preview"]) == 8
                assert all(len(model) <= 128 for model in row["model_preview"])
                assert "models" not in row and "last_error" not in row
        assert source.calls == reads and len(calls) == count


@pytest.mark.parametrize("count", SIZES)
def test_one_global_queue_after_every_worker_is_reserved(count):
    with cluster(count) as (pool, control, _, _, endpoints):
        activate(pool, control, count)
        held, outcomes = [], []
        waiter = threading.Thread(target=lambda: outcomes.append(pool.request(lambda origin: origin)))
        try:
            # Exercise the real admission/release accounting without host-thread scaling.
            held = [pool._acquire(model=None, excluded=set(), admission_timeout=0) for _ in range(count)]
            assert {state.endpoint.origin for state in held} == set(endpoints)
            assert pool.summary()["inflight"] == count
            assert pool.summary()["available_capacity"] == 0
            waiter.start()
            wait_until(lambda: pool.summary()["queue"]["waiting"] == 1)
            with pytest.raises(WorkerPoolBackpressure, match="queue is full"):
                pool.request(lambda _: pytest.fail("saturated request dispatched"))
            assert pool.summary()["queue"] == {"waiting": 1, "limit": 1, "scope": "global"}
            pool._finish(held.pop())
            waiter.join(5)
            assert not waiter.is_alive() and len(outcomes) == 1
        finally:
            for state in held:
                pool._finish(state)
            if waiter.ident is not None:
                waiter.join(5)
        assert pool.summary()["inflight"] == 0
        assert pool.summary()["available_capacity"] == count
        assert pool.summary()["queue"]["waiting"] == 0


@pytest.mark.parametrize("count", SIZES)
@pytest.mark.parametrize("change", ["remove", "revoke"])
def test_membership_change_drains_response_bearing_request_then_outage_expires(count, change):
    with cluster(count) as (pool, control, source, clock, endpoints):
        activate(pool, control, count)
        calls = []
        def sender(origin):
            calls.append(origin)
            source.value = snapshot(endpoints if change == "revoke" else tuple(
                item for item in endpoints if item != origin), clock, generation=2,
                revoked=(origin,) if change == "revoke" else ())
            control.refresh(timeout_seconds=5, probe=False)
            assert pool.summary()["draining_worker_count"] >= 1
            assert any(row.origin == origin and row.inflight == 1 for row in pool.snapshots())
            assert pool.request(lambda selected: selected) != origin
            raise ModelCallError("protocol", "response interrupted")

        with pytest.raises(ModelCallError):
            pool.request(sender, idempotent=True)
        assert len(calls) == 1 and pool.summary()["inflight"] == 0
        assert pool.status()["metrics"]["failovers"] == 0
        source.value = OSError("synthetic source outage")
        clock.now += timedelta(seconds=61)
        with pytest.raises(WorkerPoolUnavailable):
            pool.request(lambda _: pytest.fail("expired membership dispatched"))
        control.refresh(timeout_seconds=5, probe=False)
        assert pool.summary()["eligible_worker_count"] == pool.summary()["available_capacity"] == 0


@pytest.mark.parametrize("count", SIZES)
def test_model_request_targets_one_unknown_worker_without_full_refresh(count, monkeypatch):
    endpoints, probes, sent = origins(count, local=True), [], []
    pool = OllamaWorkerPool(endpoints[0], endpoints[1:], max_workers=count,
                            capability_probe_batch_size=5,
                            capability_prober=lambda origin: probes.append(origin) or {"models": []})
    monkeypatch.setattr(pool, "refresh_capabilities", lambda **_: pytest.fail("full refresh from request"))
    with pytest.raises(WorkerCapabilityUnavailable):
        pool.request(lambda origin: sent.append(origin), model="absent")
    assert len(probes) == 1 and not sent
    with pytest.raises(WorkerCapabilityUnavailable):
        pool.request(lambda origin: sent.append(origin), model="absent")
    assert len(probes) == len(set(probes)) == 2 and not sent
    pool.request(lambda origin: sent.append(origin))
    pool.status()
    assert len(probes) == 2  # model-less admission and status do not probe


@pytest.mark.parametrize("count", SIZES)
@pytest.mark.parametrize("outcome", ["success", "missing", "failure"])
def test_targeted_probe_single_flight_uses_global_queue(count, outcome):
    endpoints, calls, results, failures = origins(count, local=True), [], [], []
    entered, release = threading.Event(), threading.Event()
    def probe(origin):
        calls.append(origin)
        entered.set()
        assert release.wait(5)
        if outcome == "failure":
            raise ValueError("synthetic unavailable capability")
        return {"models": ["code"] if outcome == "success" else []}
    pool = OllamaWorkerPool(endpoints[0], endpoints[1:], max_workers=count,
                            capability_probe_parallelism=2, capability_probe_batch_size=5,
                            queue_depth=1, capability_prober=probe)
    def request():
        try:
            results.append(pool.request(lambda origin: origin, model="code"))
        except Exception as error:
            failures.append(error)
    first, second = threading.Thread(target=request), threading.Thread(target=request)
    try:
        first.start()
        assert entered.wait(5)
        second.start()
        wait_until(lambda: pool.summary()["queue"]["waiting"] == 1)
        with pytest.raises(WorkerPoolBackpressure):
            pool.request(lambda _: pytest.fail("unprobed request sent"), model="code")
        assert results == [] and len(calls) == 1
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    if outcome == "success":
        assert not failures and results == [endpoints[0], endpoints[0]]
    else:
        assert not results and len(failures) == 2
        assert all(isinstance(error, WorkerCapabilityUnavailable) for error in failures)
    assert calls == [endpoints[0]] and pool.summary()["queue"]["waiting"] == 0


@pytest.mark.parametrize("count", SIZES)
def test_server_cached_status_and_administrator_bounds(count, monkeypatch):
    import server
    endpoints = origins(count, local=True)
    entered, release, lock = threading.Event(), threading.Event(), threading.Lock()
    calls, responses = [], []
    active = peak = 0
    models = tuple("model-%04d-%s" % (i, "x" * 180) for i in range(2048))
    def probe(origin):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(active, peak)
            calls.append(origin)
            if active == 2:
                entered.set()
        assert release.wait(5)
        with lock:
            active -= 1
        return {"models": models}
    pool = OllamaWorkerPool(endpoints[0], endpoints[1:], max_workers=count,
                            capability_probe_parallelism=2, capability_probe_batch_size=5,
                            capability_prober=probe)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    with monkeypatch.context() as cached:
        def forbidden(*args, **kwargs):
            pytest.fail("default status performed detail/refresh/network work")
        for name in ("refresh_capabilities", "refresh_inventory", "status", "snapshots"):
            cached.setattr(pool, name, forbidden)
        cached.setattr(server, "_get", forbidden)
        cached.setattr(server, "_maybe_live_reload", forbidden)
        cached.setattr(server.ollama_endpoint, "open_url", forbidden)
        cached.setattr(socket, "getaddrinfo", forbidden)
        cached.setattr(socket, "create_connection", forbidden)
        status = server.status()
        assert "0/%d eligible" % count in status and "not_refreshed" in status
        assert "127.0.0.1" not in status and not calls
    monkeypatch.setattr(server, "_deployment_authenticates_callers", lambda: True)
    authorizations = []
    monkeypatch.setattr(server, "_admin_require", lambda token, role:
                        authorizations.append((token, role)) or (True, "", {"username": "fixture-admin"}))
    thread = threading.Thread(target=lambda: responses.append(
        server.ollama_pool_admin_status(token="fixture", refresh=True, page_size=128)))
    try:
        thread.start()
        assert entered.wait(5)
        assert len(calls) == 2 and peak == 2
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(responses) == 1
    assert len(calls) == 5 and peak == 2
    assert authorizations == [("fixture", "admin")]
    for argument in ("probe_batch_size", "probe_parallelism"):
        with pytest.raises(TypeError):
            server.ollama_pool_admin_status(refresh=True, **{argument: 128})
    while len(calls) < count:
        before = len(calls)
        server.ollama_pool_admin_status(token="fixture", refresh=True, page_size=1)
        assert len(calls) - before <= 5
    raw = server.ollama_pool_admin_status(token="fixture", page_size=128)
    page = json.loads(raw)
    assert len(raw.encode("utf-8")) == page["serialized_bytes"] <= 65536
    assert len(page["workers"]) <= 128
    if count >= 64:
        assert len(page["workers"]) < min(count, 128) and not page["complete"]
    assert len(calls) == count and peak <= 2


@pytest.mark.parametrize("count", SIZES)
def test_metric_identity_slots_survive_full_roster_churn(count):
    class Metrics(MetricsRegistry):
        def __init__(self):
            super().__init__(enabled=False)
            self.labels = []

        def observe_ollama_worker_request(self, *, worker, result, elapsed_seconds):
            self.labels.append(worker)

    metrics = Metrics()
    def probe(origin):
        return {"models": [origin.split("member-")[1].split(".")[0]], "max_inflight": 1}
    with cluster(count, prober=probe, metrics=metrics) as (pool, control, source, clock, endpoints):
        def dispatch_all():
            start = len(metrics.labels)
            for i, endpoint in enumerate(endpoints):
                assert pool.request(lambda origin: origin, model="%03d" % i) == endpoint
            return metrics.labels[start:]
        activate(pool, control, count)
        original = dispatch_all()
        assert original[:16] == ["w%d" % i for i in range(16)]
        assert set(original[16:]) <= {"overflow"}
        source.value = snapshot(endpoints, clock, generation=2, prefix="replacement")
        activate(pool, control, count)
        assert set(dispatch_all()) == {"overflow"}
        source.value = snapshot(tuple(reversed(endpoints)), clock, generation=3)
        activate(pool, control, count)
        assert dispatch_all() == original
        assert set(metrics.labels) == {"overflow", *("w%d" % i for i in range(16))}


def test_targeted_request_joins_administrator_batch_and_timeout_clears_waiter():
    endpoints, calls, results = origins(16, local=True), [], []
    entered, release = threading.Event(), threading.Event()
    def probe(origin):
        calls.append(origin)
        if len(calls) == 2:
            entered.set()
        assert release.wait(5)
        return {"models": ["code"]}
    pool = OllamaWorkerPool(endpoints[0], endpoints[1:], capability_prober=probe,
                            capability_probe_batch_size=5, capability_probe_parallelism=2,
                            queue_depth=1)
    admin = threading.Thread(target=pool.refresh_capabilities)
    requester = threading.Thread(target=lambda: results.append(pool.request(
        lambda origin: origin, model="code")))
    try:
        admin.start()
        assert entered.wait(5)
        with pytest.raises(WorkerPoolBackpressure, match="timed out"):
            pool.request(lambda _: pytest.fail("unknown dispatched"), model="code",
                         admission_timeout_seconds=.01)
        assert pool.summary()["queue"]["waiting"] == 0
        requester.start()
        wait_until(lambda: pool.summary()["queue"]["waiting"] == 1)
        assert len(calls) == 2 and not results
    finally:
        release.set()
        admin.join(5)
        if requester.ident is not None:
            requester.join(5)
    assert not admin.is_alive() and not requester.is_alive()
    assert len(calls) == 5 and len(results) == 1
    assert results[0] in calls and pool.summary()["queue"]["waiting"] == 0


def test_stale_model_evidence_requires_one_targeted_probe_and_no_prober_fails_closed():
    endpoints, probes = origins(16, local=True), []
    now = [100.0]
    pool = OllamaWorkerPool(endpoints[0], endpoints[1:], clock=lambda: now[0],
                            capability_probe_batch_size=16, capability_ttl_seconds=10,
                            capability_prober=lambda origin: probes.append(origin) or {"models": ["code"]})
    pool.refresh_capabilities()
    assert len(probes) == 16
    now[0] += 11
    assert pool.request(lambda origin: origin, model="code") == endpoints[0]
    assert len(probes) == 17
    no_prober = OllamaWorkerPool(endpoints[0], endpoints[1:])
    with pytest.raises(WorkerCapabilityUnavailable):
        no_prober.request(lambda _: pytest.fail("unprobed worker selected"), model="code")
