"""Lane 1 (pool-admission): streamed-turn queueing, backpressure detail, model-aware
capacity, post-send timeouts, worker attribution and per-tier status.

Each test fails on the tree before the lane's fix and passes after it.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
import urllib.error
from types import SimpleNamespace

import pytest

import master_orchestrator
import server
from sonder_runtime.adapters.inference import ollama_pool
from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerPoolBackpressure,
    local_agent_admission,
    pool_refusal_reply,
)
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.bootstrap import delegation_wiring
from sonder_runtime.interfaces.http.sse import SSEKeepAlive
from sonder_runtime.platform.runtime_threads import Thread

LOCAL = "http://127.0.0.1:11434"
NODE1 = "https://node1.example:11434"
MODEL = "qwen3.6:35b"
GIB = 1024 ** 3
_POOL_LOGGER = "sonder_runtime.adapters.inference.ollama_pool"


def _hold(pool, *, model=None, caller="agent"):
    """Start a request that holds the only slot until ``release`` is set."""
    entered, release = threading.Event(), threading.Event()

    def sender(_origin):
        entered.set()
        release.wait(10)

    thread = Thread(target=lambda: pool.request(sender, model=model, caller=caller))
    thread.start()
    assert entered.wait(2)
    return thread, release


def _node1_pool(*, node1_models=(MODEL,), local_models=("small:3b",), **kwargs):
    models = {LOCAL: list(local_models), NODE1: list(node1_models)}
    pool = OllamaWorkerPool(
        LOCAL, (NODE1,), allow_remote=True, trusted_origins=("node1.example",),
        capability_prober=lambda origin: {"models": models[origin]}, **kwargs,
    )
    pool.refresh_capabilities()
    return pool


# (a) streamed turns queue past one second; plain interactive turns still do not -------

def test_interactive_stream_waits_past_the_interactive_second_for_a_held_slot():
    pool = OllamaWorkerPool(LOCAL, max_inflight_per_worker=1, queue_depth=4)
    holder, release = _hold(pool)
    threading.Timer(1.4, release.set).start()
    started = time.monotonic()
    try:
        with local_agent_admission(caller="interactive_stream", timeout_seconds=10):
            assert pool.request(lambda origin: origin) == LOCAL
        assert time.monotonic() - started > 1.2
    finally:
        release.set()
        holder.join(3)


def test_plain_interactive_caller_still_fails_after_one_second():
    pool = OllamaWorkerPool(LOCAL, max_inflight_per_worker=1, queue_depth=4)
    holder, release = _hold(pool)
    started = time.monotonic()
    try:
        with pytest.raises(WorkerPoolBackpressure):
            pool.request(lambda origin: origin)
        assert 0.8 < time.monotonic() - started < 10
    finally:
        release.set()
        holder.join(3)


def test_stream_queue_is_bounded_by_the_turn_budget_and_a_separate_cap():
    pool = OllamaWorkerPool(
        LOCAL, max_inflight_per_worker=1, queue_depth=4,
        stream_admission_timeout_seconds=0.3,
    )
    holder, release = _hold(pool)
    try:
        started = time.monotonic()
        with local_agent_admission(caller="interactive_stream", timeout_seconds=200):
            with pytest.raises(WorkerPoolBackpressure):
                pool.request(lambda origin: origin)
        assert time.monotonic() - started < 1.0  # the pool's stream cap, not 200 s
        # The ordinary 60 s hard cap still applies to every other caller.
        with pytest.raises(ValueError):
            OllamaWorkerPool(LOCAL, local_admission_timeout_seconds=61)
    finally:
        release.set()
        holder.join(3)


def test_fleet_lane_admission_stays_bounded_by_the_local_cap_not_the_stream_cap():
    # Owner decision 1: only interactive_stream gets the 300 s cap.
    pool = OllamaWorkerPool(
        LOCAL, max_inflight_per_worker=1, queue_depth=4,
        local_admission_timeout_seconds=0.2,
    )
    holder, release = _hold(pool)
    try:
        started = time.monotonic()
        with local_agent_admission(caller="fleet", timeout_seconds=5):
            with pytest.raises(WorkerPoolBackpressure):
                pool.request(lambda origin: origin)
        assert time.monotonic() - started < 2.0
    finally:
        release.set()
        holder.join(3)


@pytest.mark.parametrize("caller,expected", [
    ("interactive_stream", "interactive"), ("fleet", "fleet"), ("agent", "agent"),
])
def test_streamed_scope_reaches_gpu_admission_with_interactive_priority(caller, expected):
    seen = []

    class Service:
        def admit(self, path, payload, **kwargs):
            seen.append(kwargs["caller"])
            from contextlib import nullcontext
            return nullcontext(object())

    pool = SimpleNamespace(gpu_admission=Service())
    with local_agent_admission(caller=caller, timeout_seconds=5):
        with ollama_pool.admitted(pool, "/api/chat", {"model": MODEL}, LOCAL):
            pass
    assert seen == [expected]


# (b) backpressure carries a position and a wait estimate ------------------------------

def test_backpressure_error_carries_queue_position_and_retry_after():
    pool = OllamaWorkerPool(LOCAL, max_inflight_per_worker=1, queue_depth=4)
    pool._states[0].inference_ewma_ms = 8000.0
    holder, release = _hold(pool)
    try:
        with pytest.raises(WorkerPoolBackpressure) as caught:
            pool.request(lambda origin: origin, admission_timeout_seconds=0.05)
    finally:
        release.set()
        holder.join(3)
    error = caught.value
    assert error.queue_position == 1
    assert error.retry_after_seconds > 1
    assert error.retry_after_seconds == pytest.approx(8.0)
    assert "queue position 1" in str(error)
    reply = pool_refusal_reply(error)
    assert "queue position 1 of" in reply and "retry in 8s" in reply


def test_pool_refusal_reaches_model_call_error_with_retry_after_and_reply(monkeypatch):
    def refuse(_path, _payload, **_kwargs):
        raise WorkerPoolBackpressure(
            "timed out waiting for Ollama worker capacity",
            retry_after_seconds=7.0, queue_position=1, queue_size=2,
        )

    monkeypatch.setattr(server, "_post", refuse)
    with pytest.raises(ModelCallError) as caught:
        server._post_model("/api/chat", {"model": MODEL}, model=MODEL, timeout=5)
    assert caught.value.retry_after_seconds == 7.0
    assert "busy" in caught.value.pool_refusal
    assert "queue position 1 of 2" in caught.value.pool_refusal
    assert "retry in 7s" in caught.value.pool_refusal


def test_waiting_stream_turn_publishes_its_queue_position_for_sse():
    pool = OllamaWorkerPool(LOCAL, max_inflight_per_worker=1, queue_depth=4)
    holder, release = _hold(pool)
    seen = []

    def wait_in_queue():
        with ollama_pool.queue_wait_tracker() as state:
            seen.append(state)
            with local_agent_admission(caller="interactive_stream", timeout_seconds=10):
                pool.request(lambda origin: origin, model=None)

    waiter = Thread(target=wait_in_queue)
    waiter.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (seen and seen[0].line()):
            time.sleep(0.01)
        assert seen[0].line() == "queued for model (position 1/1)"
    finally:
        release.set()
        holder.join(3)
        waiter.join(3)
    assert seen[0].line() is None  # admitted: nothing left to report


def test_sse_keepalive_emits_a_status_comment_while_queued():
    frames = []
    text = ["queued for qwen (position 2/3)"]
    keepalive = SSEKeepAlive(
        frames.append, 0.01, thread_factory=threading.Thread,
        status=lambda: text[0],
    ).start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not frames:
            time.sleep(0.01)
        text[0] = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b": keep-alive\n\n" not in frames[1:]:
            time.sleep(0.01)
    finally:
        keepalive.stop()
    assert b": sonder-status queued for qwen (position 2/3)\n\n" in frames
    assert b": keep-alive\n\n" in frames  # plain keep-alive resumes once admitted


# (c) a post-send read timeout is a slow generation, not a worker failure --------------

def test_post_send_timeout_leaves_failure_count_and_logs_no_failover_error(caplog):
    pool = OllamaWorkerPool(
        LOCAL, capability_prober=lambda _o: {"models": [MODEL]}, failure_threshold=2,
    )
    pool.refresh_capabilities()

    def generation_timeout(_origin):
        raise TimeoutError("timed out")  # urllib raises this bare after the send

    with caplog.at_level(logging.DEBUG, logger=_POOL_LOGGER):
        with pytest.raises(TimeoutError):
            pool.request(generation_timeout, model=MODEL, idempotent=True)
    state = pool._states[0]
    assert state.consecutive_failures == 0
    assert state.cooldown_until == 0.0
    assert pool.status()["metrics"]["slow_generation"] == 1
    assert not [r for r in caplog.records if "failing over" in r.getMessage()]


def test_connect_phase_error_still_counts_but_logs_no_failover_without_another_carrier(caplog):
    pool = OllamaWorkerPool(
        LOCAL, capability_prober=lambda _o: {"models": [MODEL]}, failure_threshold=5,
    )
    pool.refresh_capabilities()

    def refused(_origin):
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    with caplog.at_level(logging.DEBUG, logger=_POOL_LOGGER):
        with pytest.raises(urllib.error.URLError):
            pool.request(refused, model=MODEL, idempotent=True)
    assert pool._states[0].consecutive_failures == 1
    assert not [r for r in caplog.records
                if r.levelno >= logging.ERROR and "failing over" in r.getMessage()]
    assert any(r.levelno == logging.WARNING and r.exc_info is None
               and "no other worker carries" in r.getMessage() for r in caplog.records)


def test_connect_phase_error_still_fails_over_when_another_worker_carries_the_model():
    pool = _node1_pool(local_models=(MODEL,), node1_models=(MODEL,))
    seen = []

    def sender(origin):
        seen.append(origin)
        if len(seen) == 1:
            raise urllib.error.URLError(ConnectionRefusedError("refused"))
        return origin

    result = pool.request(sender, model=MODEL, idempotent=True)
    assert len(seen) == 2 and result == seen[1] and seen[0] != seen[1]


# (d) capacity counts only the workers that carry the model ----------------------------

def test_model_capacity_counts_only_the_node1_worker_that_carries_the_model():
    pool = _node1_pool()
    assert pool.model_capacity(MODEL) == 1
    assert pool.origins_for_model(MODEL) == (NODE1,)
    counts = pool.model_slot_counts(MODEL)
    assert counts["total"] == 1 and counts["workers"] == 1 and counts["remote_total"] == 1
    summary = pool.summary(model=MODEL)
    assert summary["model"] == {"name": MODEL, "workers": 1, "capacity": 1, "free": 1}
    assert pool.summary(model="absent:1b")["model"]["capacity"] is None


def test_cluster_parallel_ignores_a_local_worker_without_the_model():
    pool = _node1_pool()
    stub = SimpleNamespace(OLLAMA_POOL=pool)
    assert delegation_wiring.remote_cluster_parallel_slots(stub, MODEL) == 1
    assert delegation_wiring.effective_cluster_parallel(4, stub, MODEL) == 1
    # No model named: the historical local-plus-remote sum is unchanged.
    assert delegation_wiring.effective_cluster_parallel(4, stub) >= 4


def _pin_hardware(monkeypatch, pool):
    monkeypatch.delenv("SONDER_MAX_AGENTS", raising=False)
    monkeypatch.delenv("SONDER_PARALLEL_WORKERS", raising=False)
    monkeypatch.delenv("SONDER_MAX_WORKER_CAP", raising=False)
    monkeypatch.setattr(master_orchestrator.os, "cpu_count", lambda: 16)
    monkeypatch.setattr(master_orchestrator, "physical_memory_bytes",
                        lambda: (32 * GIB, 24 * GIB))
    monkeypatch.setattr(master_orchestrator, "gpu_memory_bytes",
                        lambda: (16 * GIB, 16 * GIB))
    monkeypatch.setattr(master_orchestrator, "fleet_model_bytes", lambda: 0)
    monkeypatch.setattr(master_orchestrator, "ollama_parallel_limit", lambda: 16)
    monkeypatch.setattr(delegation_wiring, "fleet_model_name", lambda: MODEL)
    monkeypatch.setitem(sys.modules, "server", SimpleNamespace(OLLAMA_POOL=pool))


def test_master_capacity_gives_one_slot_for_auto_and_for_use_three_agents(monkeypatch):
    pool = _node1_pool()
    _pin_hardware(monkeypatch, pool)

    auto = master_orchestrator.capacity(None)
    three = master_orchestrator.capacity(3)
    capped = master_orchestrator.capacity(3, worker_cap=3)

    assert auto["worker_slots"] == 1
    assert three["worker_slots"] == 1
    assert capped["worker_slots"] == 1
    assert capped["slot_limits"]["pool_model_capacity"] == 1
    assert capped["bound_by"] == "pool_model_capacity"
    assert "later waves" in capped["warning"]
    assert "gpu_vram" not in capped["slot_limits"]  # the local GPU is not the carrier


def test_master_capacity_follows_a_two_slot_node1(monkeypatch):
    pool = _node1_pool(max_inflight_by_origin={NODE1: 2})
    _pin_hardware(monkeypatch, pool)
    assert pool.model_capacity(MODEL) == 2
    assert master_orchestrator.capacity(3, worker_cap=3)["worker_slots"] == 2
    assert master_orchestrator.capacity(None)["worker_slots"] == 2


def test_master_capacity_is_unchanged_when_no_worker_advertises_the_model(monkeypatch):
    pool = _node1_pool(node1_models=("other:1b",), local_models=("small:3b",))
    _pin_hardware(monkeypatch, pool)
    report = master_orchestrator.capacity(None)
    assert "pool_model_capacity" not in report["slot_limits"]


# per-worker slot setting (decision 2) --------------------------------------------------

def test_per_origin_slot_setting_changes_only_that_worker():
    pool = _node1_pool(max_inflight_by_origin={NODE1: 2})
    rows = {row["origin"]: row["capacity"] for row in pool.pool_capacity_summary()["workers"]}
    assert rows == {LOCAL: 1, NODE1: 2}
    default = _node1_pool()
    assert {row["capacity"] for row in default.pool_capacity_summary()["workers"]} == {1}


def test_per_origin_slot_setting_parses_and_rejects_bad_input():
    from sonder_runtime.adapters.inference.ollama_pool import parse_origin_slots

    assert parse_origin_slots("https://node1.example:11434=2") == {NODE1: 2}
    assert parse_origin_slots("") == {}
    for bad in ("https://node1.example:11434", "https://node1.example:11434=0",
                "https://node1.example:11434=x", "=2"):
        with pytest.raises(ValueError):
            parse_origin_slots(bad)
    with pytest.raises(ValueError):
        OllamaWorkerPool(LOCAL, max_inflight_by_origin={NODE1: 2})  # not a pool worker


# (e) a Node1 failure names Node1 -------------------------------------------------------

def _node1_http_500(monkeypatch):
    pool = _node1_pool(local_models=("small:3b",))
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)

    def post(path, payload, **_kwargs):
        def sender(_origin):
            raise urllib.error.HTTPError(NODE1 + path, 500, "boom", {}, None)
        return pool.request(sender, model=MODEL, payload=payload)

    monkeypatch.setattr(server, "_post", post)
    with pytest.raises(ModelCallError) as caught:
        server._post_model("/api/chat", {"model": MODEL}, model=MODEL, timeout=5)
    return caught.value


def test_node1_http_500_formats_with_the_node1_origin_not_local_ollama(monkeypatch):
    error = _node1_http_500(monkeypatch)
    assert error.served_origin == NODE1
    assert error.worker_id
    with role_tool_policy.caller_role_scope(None):
        text = server._format_model_call_error(error)
    assert NODE1 in text and "remote Ollama" in text
    assert "local Ollama" not in text and "HTTP 500" in text


def test_account_caller_sees_a_generic_remote_worker_not_the_address(monkeypatch):
    error = _node1_http_500(monkeypatch)
    with role_tool_policy.caller_role_scope("user"):
        text = server._format_model_call_error(error)
    assert "remote worker" in text
    assert "node1.example" not in text and "local Ollama" not in text


def test_error_stamped_by_request_primary_names_the_primary_origin():
    pool = OllamaWorkerPool(LOCAL)

    def failing(_origin):
        raise urllib.error.URLError("down")

    with pytest.raises(urllib.error.URLError) as caught:
        pool.request_primary(failing)
    assert caught.value.sonder_worker_origin == LOCAL


def test_work_narration_names_the_carriers_for_admins_and_hides_them_from_accounts():
    from sonder_runtime.adapters.observability.work_narration_facts import configured_host

    pool = _node1_pool()
    runtime = SimpleNamespace(
        BASE=LOCAL, OLLAMA_POOL=pool, _RUNTIME_POLICY={"local_models": {"code": MODEL}},
    )
    with role_tool_policy.caller_role_scope(None):
        assert configured_host(runtime, "code") == "ollama node1.example:11434"
    assert configured_host(runtime, "code") == "ollama 1 worker"  # unbound: generic
    with role_tool_policy.caller_role_scope("user"):
        assert configured_host(runtime, "code") == "ollama 1 worker"
    lone = SimpleNamespace(BASE=LOCAL, OLLAMA_POOL=OllamaWorkerPool(LOCAL))
    assert configured_host(lone, "code") == "ollama 127.0.0.1:11434"


def _run_unbound_thread(fn):
    """Run ``fn`` on a plain thread that did not copy this thread's context."""
    out = {}

    def target():
        out["value"] = fn()

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(5)
    return out["value"]


@pytest.mark.parametrize("role", ["user", None])
def test_worker_thread_without_a_copied_context_never_sees_worker_hosts(monkeypatch, role):
    from sonder_runtime.adapters.observability.work_narration_facts import configured_host

    error = _node1_http_500(monkeypatch)
    runtime = SimpleNamespace(
        BASE=LOCAL, OLLAMA_POOL=_node1_pool(),
        _RUNTIME_POLICY={"local_models": {"code": MODEL}},
    )
    with role_tool_policy.caller_role_scope(role):
        text = _run_unbound_thread(lambda: server._format_model_call_error(error))
        host = _run_unbound_thread(lambda: configured_host(runtime, "code"))
    assert "remote worker" in text and "node1.example" not in text
    assert host == "ollama 1 worker"


# (g) the backpressure text carries the queue position and never the holder note --------

_HOLDER_WORDS = ("held by", "lease #", "caller=", "phase=", "age=")


def test_admission_timeout_text_has_queue_position_but_not_the_holder_note(caplog):
    pool = OllamaWorkerPool(LOCAL, max_inflight_per_worker=1, queue_depth=4)
    holder, release = _hold(pool, caller="agent")
    try:
        with caplog.at_level(logging.WARNING, logger=_POOL_LOGGER):
            with pytest.raises(WorkerPoolBackpressure) as caught:
                pool.request(lambda origin: origin, admission_timeout_seconds=0.05)
    finally:
        release.set()
        holder.join(3)
    text = str(caught.value)
    assert "queue position 1 of 1" in text
    assert not any(word in text for word in _HOLDER_WORDS)
    assert (caught.value.queue_position, caught.value.queue_size) == (1, 1)
    logged = [r.getMessage() for r in caplog.records if "admission timeout" in r.getMessage()]
    assert logged and "held by lease #" in logged[0]


def test_post_model_error_detail_does_not_leak_the_holder_note(monkeypatch):
    pool = OllamaWorkerPool(LOCAL, max_inflight_per_worker=1, queue_depth=4)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)

    def post(path, payload, **_kwargs):
        return pool.request(
            lambda origin: origin, model=None, payload=payload, admission_timeout_seconds=0.05,
        )

    monkeypatch.setattr(server, "_post", post)
    holder, release = _hold(pool, caller="agent")
    try:
        with pytest.raises(ModelCallError) as caught:
            server._post_model("/api/chat", {"model": MODEL}, model=MODEL, timeout=5)
    finally:
        release.set()
        holder.join(3)
    shown = str(caught.value.detail) + "\n" + server._format_model_call_error(caught.value)
    assert not any(word in shown for word in _HOLDER_WORDS)


@pytest.mark.parametrize("queue_depth", [0, 1])
def test_queue_full_refusal_carries_no_queue_position(queue_depth):
    pool = OllamaWorkerPool(LOCAL, max_inflight_per_worker=1, queue_depth=queue_depth)
    holder, release = _hold(pool)
    waiter = None
    try:
        if queue_depth:
            waiter = Thread(target=lambda: pool.request(
                lambda origin: origin, admission_timeout_seconds=5))
            waiter.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and pool._waiters < queue_depth:
                time.sleep(0.01)
        with pytest.raises(WorkerPoolBackpressure) as caught:
            pool.request(lambda origin: origin, admission_timeout_seconds=0.5)
    finally:
        release.set()
        holder.join(3)
        if waiter is not None:
            waiter.join(6)
    assert caught.value.queue_position is None and caught.value.queue_size is None
    assert "queue position" not in pool_refusal_reply(caught.value)


# (h) the queue wait reaches the client over the wire, and is cleared -------------------

def test_sse_keepalive_sends_one_clear_frame_when_the_status_ends():
    frames = []
    text = ["queued for qwen (position 1/1)"]
    keepalive = SSEKeepAlive(
        frames.append, 0.01, thread_factory=threading.Thread, status=lambda: text[0],
    ).start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not frames:
            time.sleep(0.01)
        text[0] = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if b": sonder-status\n\n" in frames and b": keep-alive\n\n" in frames[
                frames.index(b": sonder-status\n\n"):
            ]:
                break
            time.sleep(0.01)
    finally:
        keepalive.stop()
    shown = frames.index(b": sonder-status queued for qwen (position 1/1)\n\n")
    assert frames.count(b": sonder-status\n\n") == 1
    assert frames.index(b": sonder-status\n\n") > shown
    assert b": keep-alive\n\n" in frames[frames.index(b": sonder-status\n\n"):]


def test_sse_keepalive_stop_clears_a_status_still_shown():
    frames = []
    keepalive = SSEKeepAlive(
        frames.append, 0.01, thread_factory=threading.Thread, status=lambda: "queued",
    ).start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not frames:
        time.sleep(0.01)
    assert keepalive.stop()
    assert frames[-1] == b": sonder-status\n\n"


def test_sse_keepalive_without_a_status_never_sends_a_status_frame():
    frames = []
    keepalive = SSEKeepAlive(frames.append, 0.01, thread_factory=threading.Thread).start()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not frames:
        time.sleep(0.01)
    keepalive.stop()
    assert frames and all(frame == b": keep-alive\n\n" for frame in frames)


def test_streamed_turn_queued_on_the_pool_sends_status_then_clear(monkeypatch):
    import sonder_runtime.interfaces.http.serve as ts
    from tests.test_serve_sse_keepalive import _http_server, _stream_request

    pool = _node1_pool(max_inflight_per_worker=1, queue_depth=4)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(
        server, "_post",
        lambda path, payload, **_kw: pool.request(
            lambda origin: {"ok": True}, model=MODEL, payload=payload),
    )
    holder, release = _hold(pool, model=MODEL)

    def answer(*_args, **_kwargs):
        with local_agent_admission(caller="interactive_stream", timeout_seconds=20):
            server._post_model("/api/chat", {"model": MODEL}, model=MODEL, timeout=20)
        return "late answer"

    def release_once_queued(received):
        if b": sonder-status queued for " in received:
            release.set()

    monkeypatch.setattr(ts._legacy_runtime(), "answer_with_history", answer)
    try:
        with _http_server(monkeypatch) as port:
            text = _stream_request(port, on_received=release_once_queued)
    finally:
        release.set()
        holder.join(3)
    _head, _, stream = text.partition("\r\n\r\n")
    lines = stream.splitlines()
    statuses = [i for i, line in enumerate(lines) if line.startswith(": sonder-status")]
    assert statuses and lines[statuses[0]].startswith(": sonder-status queued for ")
    assert lines[statuses[-1]] == ": sonder-status"
    answer_at = next(i for i, line in enumerate(lines) if line.startswith("data: ") and "late answer" in line)
    assert statuses[-1] < answer_at


# (f) status reports capacity per local tier model ---------------------------------------

def test_status_prints_one_line_per_local_tier_model(monkeypatch):
    pool = _node1_pool()
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", {
        "local_models": {"code": MODEL, "sonder": "small:3b", "fast": "absent:1b"},
    })
    text = server.status()
    assert "code (qwen3.6:35b): 1 worker, capacity=1" in text
    assert "sonder (small:3b): 1 worker, capacity=1" in text
    assert "fast (absent:1b): no worker advertises it yet" in text


def test_repl_pool_words_carry_the_same_per_model_counts(monkeypatch):
    from sonder_runtime.interfaces.repl import repl

    pool = _node1_pool()
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", {"local_models": {"code": MODEL}})
    words = repl._pool_words()
    assert words.startswith("Ollama: 2 workers")
    assert "code (qwen3.6:35b): 1 worker, capacity=1" in words


def test_error_formatting_defaults_to_hiding_a_remote_worker_host():
    from sonder_runtime.adapters.model_error_formatting import format_runtime_model_call_error

    error = ModelCallError("timeout", "slow")
    error.served_origin = "http://203.0.113.9:11434"
    text = format_runtime_model_call_error(error, endpoint_loopback=False, display="primary")
    assert "203.0.113.9" not in text
