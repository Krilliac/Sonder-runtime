"""Pool slot leases: release on every exit path, attribution, GPU-queue parking.

A request's pool slot used to leak when the sender raised a BaseException or an
evidence check raised outside its caught tuple, and a local request queued in
GPU admission held the loopback primary's only slot for the whole queue wait,
so cloud models that need no GPU were refused with backpressure.
"""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference import ollama_pool
from sonder_runtime.adapters.inference.gpu_admission import GpuAdmissionRefused
from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerPoolBackpressure,
    WorkerPoolUnavailable,
    _safe_error,
)
from sonder_runtime.domain.gpu_admission import (
    HostMemoryReading,
    Refused,
    RefusalReason,
)
from sonder_runtime.platform.runtime_threads import Thread
from tests.support.gpu_admission_harness import (
    GIB,
    MODEL as LOCAL,
    FakeOllama,
    make_service,
    wait_until,
)

PRIMARY = "http://127.0.0.1:11434"
WORKER = "https://10.77.0.2:8443"
CLOUD = "glm-5.2:cloud"
POOL_LOGGER = "sonder_runtime.adapters.inference.ollama_pool"
TOOLS_PAYLOAD = {
    "model": "kimi-k3:cloud",
    "messages": [{"role": "user", "content": "x"}],
    "tools": [{"type": "function", "function": {"name": "t"}}],
}


class _Metrics:
    def __init__(self):
        self.requests = []
        self.circuits = []

    def observe_ollama_worker_request(self, *, worker, result, elapsed_seconds):
        self.requests.append((worker, result))

    def observe_ollama_worker_circuit(self, *, worker, state):
        self.circuits.append((worker, state))


def _models(origin):
    if origin == PRIMARY:
        return {"models": ["kimi-k3:cloud", CLOUD, LOCAL]}
    return {"models": ["sonder:latest"]}


def _pool(*, workers=(WORKER,), **kwargs):
    kwargs.setdefault("gpu_admission", SimpleNamespace(mode="off"))
    kwargs.setdefault("metrics", _Metrics())
    pool = OllamaWorkerPool(
        PRIMARY, workers, allow_remote=bool(workers), trusted_origins=workers,
        capability_prober=_models, **kwargs)
    pool.refresh_capabilities()
    return pool


def _primary(pool):
    return next(state for state in pool._states if state.endpoint.origin == PRIMARY)


def _lease(pool, *, model, caller="agent"):
    """Acquire one slot with its lease, as ``request`` does, without dispatching."""
    box = []
    pool._acquire(model=model, excluded=set(), admission_timeout=0,
                  _lease_box=box, _lease_caller=caller)
    return box[0]


def _assert_released(pool):
    for state in pool._states:
        assert state.inflight == 0, state.endpoint.worker_id
    assert pool.summary()["inflight"] == 0


class _Abort(BaseException):
    """A non-Exception BaseException, like a cancellation signal."""


class _Evidence:
    """Recent-evidence double whose second freshness read raises KeyError."""

    revision = None

    def __init__(self, path):
        self.path = path
        self.calls = 0

    def has_fresh_failure(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls >= 2:
            raise KeyError("checked_at")
        return False


class _StrictEvidence(_Evidence):
    def has_fresh_failure(self, *_args, **_kwargs):
        return False


# -- A: every exit path closes the lease ------------------------------------------


@pytest.mark.parametrize(
    "exception",
    [KeyboardInterrupt, SystemExit, GeneratorExit, _Abort],
    ids=["KeyboardInterrupt", "SystemExit", "GeneratorExit", "custom"],
)
def test_base_exception_from_sender_releases_the_slot(exception):
    pool = _pool()

    def sender(_origin):
        raise exception("stop")

    with pytest.raises(exception):
        pool.request(sender, model="kimi-k3:cloud")

    primary = _primary(pool)
    _assert_released(pool)
    assert primary.consecutive_failures == 0
    assert pool._metrics["transport_failures"] == 0
    assert pool.request(lambda origin: origin, model=CLOUD,
                        admission_timeout_seconds=0.2) == PRIMARY


def test_pre_dispatch_evidence_key_error_releases_the_slot(tmp_path):
    pool = _pool(recent_evidence=_Evidence(tmp_path / "none.json"),
                 capability_routing="advisory")

    with pytest.raises(KeyError):
        pool.request(lambda _origin: "ok", model="kimi-k3:cloud",
                     payload=dict(TOOLS_PAYLOAD))

    _assert_released(pool)
    assert pool.request(lambda origin: origin, model=CLOUD,
                        admission_timeout_seconds=0.2) == PRIMARY


def test_strict_post_response_lookup_error_releases_the_slot(tmp_path, monkeypatch):
    import sonder_runtime.application.routing.request_capabilities as capabilities

    pool = _pool(recent_evidence=_StrictEvidence(tmp_path / "none.json"),
                 identity_for=lambda _origin, _model, _payload: None,
                 capability_routing="strict")
    calls = {"n": 0}

    def flaky_check(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise LookupError("evidence record vanished")
        return True, "passed"

    monkeypatch.setattr(capabilities, "check_request_evidence", flaky_check)
    with pytest.raises(LookupError):
        pool.request(lambda _origin: "200 OK body", model="kimi-k3:cloud",
                     payload=dict(TOOLS_PAYLOAD))

    assert calls["n"] == 3
    _assert_released(pool)


def test_inflight_matches_open_counted_leases_after_mixed_outcomes():
    pool = _pool()
    assert pool.request(lambda origin: "ok") == "ok"

    seen = []

    def fail_first(origin):
        seen.append(origin)
        if len(seen) == 1:
            raise URLError("connection refused")
        return origin

    assert pool.request(fail_first, idempotent=True) == seen[1]
    assert len(seen) == 2 and seen[0] != seen[1]

    def not_retryable(_origin):
        raise ValueError("model error")

    with pytest.raises(ValueError):
        pool.request(not_retryable, model="kimi-k3:cloud")

    def abort(_origin):
        raise _Abort("stop")

    with pytest.raises(_Abort):
        pool.request(abort, model="kimi-k3:cloud")

    _assert_released(pool)
    leases = pool.summary()["leases"]
    assert leases["held"] == 0
    assert leases["reclaimed"] == 0
    assert leases["oldest_age_seconds"] is None
    assert pool._leases == {}


# -- B: attribution and reclaim -------------------------------------------------


def test_watchdog_warns_once_about_a_long_held_lease(caplog):
    now = [1000.0]
    pool = _pool(clock=lambda: now[0])
    entered = threading.Event()
    release = threading.Event()

    def hold(_origin):
        entered.set()
        release.wait(5)
        return "done"

    holder = Thread(
        target=lambda: pool.request(hold, model="kimi-k3:cloud", caller="agent"),
        name="lease-holder-thread", daemon=True)
    holder.start()
    try:
        assert entered.wait(2)
        now[0] += 121.0
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=POOL_LOGGER):
            first = pool.summary()
            pool.summary()
        warnings = [record for record in caplog.records
                    if record.name == POOL_LOGGER and record.levelno == logging.WARNING]
        assert len(warnings) == 1, [record.getMessage() for record in warnings]
        message = warnings[0].getMessage()
        assert "127.0.0.1:11434" in message
        assert "kimi-k3:cloud" in message
        assert "caller=agent" in message
        assert "phase=" in message
        assert "lease-holder-thread" in message
        assert first["leases"]["held"] == 1
        assert first["leases"]["oldest_age_seconds"] >= 120
        # A sender that never enters admitted() stays in the dispatching phase.
        assert first["leases"]["oldest_phase"] == "dispatching"
        assert first["leases"]["reclaimed"] == 0
    finally:
        release.set()
        holder.join(5)
    _assert_released(pool)


def test_lease_of_a_dead_thread_is_reclaimed_once(caplog):
    pool = _pool()

    def open_and_exit():
        _lease(pool, model="kimi-k3:cloud")

    worker = Thread(target=open_and_exit, name="lease-orphan-thread", daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert _primary(pool).inflight == 1

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=POOL_LOGGER):
        summary = pool.summary()
        again = pool.summary()
    errors = [record for record in caplog.records
              if record.name == POOL_LOGGER and record.levelno == logging.ERROR]
    assert len(errors) == 1, [record.getMessage() for record in errors]
    assert "lease-orphan-thread" in errors[0].getMessage()
    assert summary["inflight"] == 0
    assert summary["leases"]["held"] == 0
    assert summary["leases"]["reclaimed"] == 1
    assert again["leases"]["reclaimed"] == 1
    assert _primary(pool).inflight == 0
    assert pool.request(lambda origin: origin, model=CLOUD,
                        admission_timeout_seconds=0.2) == PRIMARY


def test_admission_timeout_names_the_lease_holding_the_slot(caplog):
    pool = _pool()
    entered = threading.Event()
    release = threading.Event()

    def hold(_origin):
        entered.set()
        release.wait(5)
        return "done"

    holder = Thread(target=lambda: pool.request(hold, model=LOCAL, caller="agent"),
                    name="slot-holder", daemon=True)
    holder.start()
    try:
        assert entered.wait(2)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=POOL_LOGGER):
            with pytest.raises(WorkerPoolBackpressure):
                pool.request(lambda origin: origin, model=CLOUD,
                             admission_timeout_seconds=0.05)
        timeouts = [record.getMessage() for record in caplog.records
                    if "admission timeout" in record.getMessage()]
        assert len(timeouts) == 1
        assert "held by lease #" in timeouts[0]
        assert "caller=agent" in timeouts[0]
        assert ("model=%s" % LOCAL) in timeouts[0]
        assert "phase=dispatching" in timeouts[0]
    finally:
        release.set()
        holder.join(5)
    _assert_released(pool)


def test_summary_lease_block_is_scalar_only():
    pool = _pool()
    summary = pool.summary()
    assert summary["schema_version"] == 2
    assert summary["leases"] == {
        "held": 0, "oldest_age_seconds": None, "oldest_phase": None, "reclaimed": 0,
    }


# -- C: a request queued in GPU admission does not hold the pool slot -----------


_ROOMY = HostMemoryReading(
    available_physical_bytes=64 * GIB, total_physical_bytes=64 * GIB,
    commit_limit_bytes=96 * GIB, commit_available_bytes=64 * GIB, commit_enforced=True)


def _local_body():
    return {"model": LOCAL, "stream": False, "keep_alive": -1,
            "messages": [{"role": "user", "content": "hi"}], "options": {"num_ctx": 8192}}


class _LocalCall:
    """A background local request whose sender enters ``ollama_pool.admitted``."""

    def __init__(self, pool, *, budget=30.0):
        self.pool = pool
        self.transport = []
        self.outcome = {}
        body = _local_body()
        deadline = time.monotonic() + budget

        def sender(origin):
            with ollama_pool.admitted(pool, "/api/chat", body, origin, deadline=deadline):
                self.transport.append((origin, _primary(pool).inflight))
                return {"ok": True}

        def run():
            try:
                self.outcome["value"] = pool.request(sender, model=LOCAL, payload=body)
            except BaseException as error:  # the test inspects every outcome
                self.outcome["error"] = error

        self.thread = Thread(target=run, name="local-gpu-call", daemon=True)
        self.thread.start()

    def join(self, timeout=10.0):
        self.thread.join(timeout)
        assert not self.thread.is_alive()


def _queued_pool(tmp_path, **pool_kwargs):
    service = make_service(
        tmp_path, FakeOllama(), host=(3 * GIB, 2 * GIB), on_host_floor="queue",
        max_queue_wait_seconds=3, host_recheck_interval_ms=100)
    return _pool(workers=(), gpu_admission=service, **pool_kwargs), service


def _phase(pool):
    return pool.summary()["leases"]["oldest_phase"]


def _parked(pool):
    return _phase(pool) == "gpu_admission" and _primary(pool).inflight == 0


def test_gpu_queued_local_request_does_not_block_cloud_on_the_primary(tmp_path):
    pool, service = _queued_pool(tmp_path)
    call = _LocalCall(pool)
    try:
        assert wait_until(lambda: _phase(pool) == "gpu_admission"
                          and service.snapshot()["queued"] >= 1, timeout=2.0)
        assert pool.request(lambda origin: origin, model=CLOUD) == PRIMARY
        assert _primary(pool).inflight == 0
        assert pool.summary()["leases"]["held"] == 1
    finally:
        call.join()
    assert isinstance(call.outcome.get("error"), GpuAdmissionRefused)
    assert call.transport == []
    _assert_released(pool)
    assert pool.summary()["leases"]["held"] == 0
    assert service._the_store().leases() == ()


def test_gpu_queued_local_request_proceeds_when_host_memory_recovers(tmp_path):
    pool, service = _queued_pool(tmp_path)
    call = _LocalCall(pool)
    try:
        assert wait_until(lambda: _parked(pool), timeout=2.0)
        service._host_reader = lambda: _ROOMY
    finally:
        call.join()
    assert call.outcome.get("value") == {"ok": True}, call.outcome
    assert call.transport == [(PRIMARY, 1)]
    _assert_released(pool)
    assert pool.summary()["leases"]["held"] == 0
    assert service._the_store().leases() == ()


def _hold_primary(pool):
    entered = threading.Event()
    release = threading.Event()
    result = {}

    def hold(origin):
        entered.set()
        release.wait(10)
        return origin

    def run():
        result["value"] = pool.request(hold, model=CLOUD)

    thread = Thread(target=run, name="cloud-holder", daemon=True)
    thread.start()
    assert entered.wait(2)
    return thread, release, result


def test_unparking_request_waits_for_the_slot_then_proceeds(tmp_path):
    pool, service = _queued_pool(tmp_path)
    call = _LocalCall(pool)
    holder = release = None
    try:
        assert wait_until(lambda: _parked(pool), timeout=2.0)
        holder, release, held = _hold_primary(pool)
        assert _primary(pool).inflight == 1
        service._host_reader = lambda: _ROOMY
        assert wait_until(lambda: _primary(pool).unpark_waiters == 1, timeout=3.0)
        assert _primary(pool).inflight == 1
        assert call.transport == []
        release.set()
        holder.join(5)
        assert held["value"] == PRIMARY
    finally:
        if release is not None:
            release.set()
        call.join()
    assert call.outcome.get("value") == {"ok": True}, call.outcome
    assert call.transport == [(PRIMARY, 1)]
    _assert_released(pool)
    assert _primary(pool).unpark_waiters == 0
    assert service._the_store().leases() == ()


def test_unparking_request_gets_backpressure_at_its_deadline(tmp_path):
    pool, service = _queued_pool(tmp_path)
    call = _LocalCall(pool, budget=6.0)
    holder = release = None
    rejections = pool._metrics["backpressure_rejections"]
    try:
        assert wait_until(lambda: _parked(pool), timeout=4.0)
        holder, release, _held = _hold_primary(pool)
        service._host_reader = lambda: _ROOMY
        assert wait_until(lambda: _primary(pool).unpark_waiters == 1, timeout=4.0)
        assert _primary(pool).inflight == 1
        call.join(8)
        assert _primary(pool).inflight == 1
    finally:
        if release is not None:
            release.set()
        if holder is not None:
            holder.join(5)
        call.join()
    assert isinstance(call.outcome.get("error"), WorkerPoolBackpressure), call.outcome
    assert call.transport == []
    assert pool._metrics["backpressure_rejections"] == rejections + 1
    _assert_released(pool)
    assert _primary(pool).unpark_waiters == 0
    assert pool.summary()["leases"]["held"] == 0
    assert service._the_store().leases() == ()


def test_fresh_requests_queue_behind_an_unparking_request():
    pool = _pool(workers=())
    primary = _primary(pool)
    with pool._condition:
        primary.unpark_waiters = 1
    try:
        with pytest.raises(WorkerPoolBackpressure):
            pool.request(lambda origin: origin, model=CLOUD, admission_timeout_seconds=0.05)
    finally:
        with pool._condition:
            primary.unpark_waiters = 0
    assert pool.request(lambda origin: origin, model=CLOUD) == PRIMARY


def test_on_queue_runs_once_and_a_failing_callback_does_not_break_admission(tmp_path):
    service = make_service(
        tmp_path, FakeOllama(), host=(3 * GIB, 2 * GIB), on_host_floor="queue",
        max_queue_wait_seconds=10, host_recheck_interval_ms=100)
    assert service.accepts_queue_callback is True
    low = service._host_reader
    reads = []

    def host_reader():
        # Below the floor for three rechecks, so the request queues repeatedly.
        reads.append(1)
        return low() if len(reads) <= 3 else _ROOMY

    service._host_reader = host_reader
    calls = []

    def on_queue():
        calls.append(time.monotonic())
        raise RuntimeError("callback failure")

    admitted = []
    with service.admit("/api/chat", _local_body(), origin=PRIMARY,
                       deadline=time.monotonic() + 20, on_queue=on_queue):
        admitted.append(True)
    assert admitted == [True]
    assert len(calls) == 1
    assert service.snapshot()["queued"] >= 3
    assert service._the_store().leases() == ()


def test_unpark_wait_is_capped_and_releases_the_gpu_slot(tmp_path):
    pool, service = _queued_pool(tmp_path, local_admission_timeout_seconds=0.5)
    call = _LocalCall(pool, budget=30.0)
    holder = release = None
    try:
        assert wait_until(lambda: _parked(pool), timeout=2.0)
        holder, release, _held = _hold_primary(pool)
        service._host_reader = lambda: _ROOMY
        assert wait_until(lambda: _primary(pool).unpark_waiters == 1, timeout=3.0)
        started = time.monotonic()
        call.thread.join(4.0)
        assert not call.thread.is_alive(), "unpark waited past its cap"
        assert time.monotonic() - started < 3.0  # cap 0.5 s; the deadline is 30 s
        # The GPU slot is free again while the other request still holds the pool slot.
        assert service._the_store().leases() == ()
        assert _primary(pool).inflight == 1
    finally:
        if release is not None:
            release.set()
        if holder is not None:
            holder.join(5)
        call.join()
    assert isinstance(call.outcome.get("error"), WorkerPoolBackpressure), call.outcome
    assert "retry" in str(call.outcome["error"])
    assert call.transport == []
    _assert_released(pool)
    assert _primary(pool).unpark_waiters == 0


@pytest.mark.parametrize("trip", ["cooldown", "half_open"])
def test_unpark_refuses_a_worker_that_tripped_while_parked(tmp_path, trip):
    pool, service = _queued_pool(tmp_path)
    call = _LocalCall(pool)
    try:
        assert wait_until(lambda: _parked(pool), timeout=2.0)
        primary = _primary(pool)
        with pool._condition:
            primary.consecutive_failures = pool._failure_threshold
            if trip == "cooldown":
                primary.cooldown_until = pool._clock() + 30.0
            else:
                primary.half_open_inflight = True  # another request is the probe
        service._host_reader = lambda: _ROOMY
    finally:
        call.join()
    assert isinstance(call.outcome.get("error"), WorkerPoolUnavailable), call.outcome
    assert call.transport == []
    assert _primary(pool).inflight == 0
    if trip == "half_open":
        # The refused request did not own the probe, so it must not end it.
        assert _primary(pool).half_open_inflight is True
    assert pool.summary()["leases"]["held"] == 0
    assert service._the_store().leases() == ()


class _VanishedThread:
    """A ``_DummyThread`` on Python 3.14 raises from ``is_alive`` once it is gone."""

    name = "Dummy-7"

    def is_alive(self):
        raise RuntimeError("thread is not alive")


def _lease_on_vanished_thread(pool):
    lease = _lease(pool, model=CLOUD)
    lease.thread = _VanishedThread()
    return lease


def test_vanished_thread_lease_is_reclaimed_by_summary():
    pool = _pool(workers=())
    _lease_on_vanished_thread(pool)
    summary = pool.summary()
    assert summary["leases"]["reclaimed"] == 1
    assert summary["leases"]["held"] == 0
    assert summary["inflight"] == 0


def test_vanished_thread_lease_is_reclaimed_at_admission_timeout():
    pool = _pool(workers=())
    _lease_on_vanished_thread(pool)
    assert pool.request(lambda origin: origin, model=CLOUD,
                        admission_timeout_seconds=0.05) == PRIMARY
    assert pool._waiters == 0
    _assert_released(pool)
    assert pool.summary()["leases"]["reclaimed"] == 1


def test_exception_right_after_acquire_does_not_orphan_the_slot(monkeypatch):
    pool = _pool(workers=())
    real_acquire = pool._acquire

    def acquire_then_abort(**kwargs):
        real_acquire(**kwargs)
        raise _Abort("between acquire and dispatch")

    monkeypatch.setattr(pool, "_acquire", acquire_then_abort)
    with pytest.raises(_Abort):
        pool.request(lambda origin: origin, model=CLOUD)
    monkeypatch.undo()
    _assert_released(pool)
    assert pool.summary()["leases"]["held"] == 0
    assert pool.request(lambda origin: origin, model=CLOUD,
                        admission_timeout_seconds=0.2) == PRIMARY


def test_membership_change_keeps_a_member_whose_lease_is_parked():
    from tests.test_inference_membership_acceptance import activate, cluster, snapshot

    with cluster(16) as (pool, control, source, clock, endpoints):
        activate(pool, control, 16)
        seen = {}

        def sender(origin):
            _owner, lease = ollama_pool._ACTIVE_LEASE.get()
            pool._park_lease(lease)
            source.value = snapshot(
                tuple(item for item in endpoints if item != origin), clock, generation=2)
            control.refresh(timeout_seconds=5, probe=False)
            seen["retained"] = any(state is lease.state for state in pool._states)
            seen["membership"] = lease.state.membership_state
            seen["state"] = lease.state
            return origin

        pool.request(sender)
        assert seen["retained"] is True
        assert seen["membership"] == "draining"
        assert not any(state is seen["state"] for state in pool._states)
        assert pool.summary()["inflight"] == 0


# -- D: refusals keep their reason in pool diagnostics --------------------------


def test_safe_error_names_the_gpu_admission_refusal_reason():
    error = GpuAdmissionRefused(Refused(RefusalReason.HOST_RAM_FLOOR, "3.0 GiB RAM available"))
    assert _safe_error(error).startswith("GpuAdmissionRefused: host_ram_floor")
