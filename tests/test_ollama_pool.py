from __future__ import annotations

from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    from_environment,
    parse_worker_origins,
    validate_worker_origin,
)


def test_worker_origin_parser_accepts_comma_and_semicolon_lists():
    assert parse_worker_origins("https://a:11434; https://b:11434, https://a:11434") == (
        "https://a:11434", "https://b:11434", "https://a:11434",
    )


def test_remote_worker_requires_https_and_explicit_consent():
    with pytest.raises(ValueError, match="require SONDER_ALLOW_REMOTE_OLLAMA"):
        validate_worker_origin("http://192.168.1.20:11434", allow_remote=False)
    with pytest.raises(ValueError, match="must use https"):
        validate_worker_origin("http://192.168.1.20:11434", allow_remote=True)
    assert validate_worker_origin(
        "https://192.168.1.20:11434/", allow_remote=True
    ) == "https://192.168.1.20:11434"


def test_pool_fails_over_only_on_transport_failure():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("https://worker-a.example:11434", "https://worker-b.example:11434"),
        allow_remote=True,
    )
    calls = []

    def send(origin):
        calls.append(origin)
        if origin == "http://127.0.0.1:11434":
            raise URLError("primary unavailable")
        return {"worker": origin}

    result = pool.request(send)

    assert result["worker"] != "http://127.0.0.1:11434"
    assert calls[0] == "http://127.0.0.1:11434"
    assert len(calls) == 2
    assert all(snapshot.consecutive_failures == 0 for snapshot in pool.snapshots() if snapshot.origin != calls[0])


def test_pool_does_not_fail_over_after_a_non_transport_failure():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434", ("http://127.0.0.2:11434",)
    )
    calls = []

    def send(origin):
        calls.append(origin)
        raise ValueError("invalid request")

    with pytest.raises(ValueError, match="invalid request"):
        pool.request(send)
    assert len(calls) == 1


def test_pool_circuit_breaker_cools_a_repeatedly_failed_worker():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("http://127.0.0.2:11434",),
        failure_threshold=1,
        cooldown_seconds=60,
    )
    calls = []

    def send(origin):
        calls.append(origin)
        if origin == "http://127.0.0.1:11434":
            raise URLError("primary unavailable")
        return origin

    pool.request(send)
    pool.request(send)
    status = pool.status()
    primary = next(item for item in status["workers"] if item["origin"].endswith(".1:11434"))
    assert primary["healthy"] is False
    assert primary["consecutive_failures"] == 1
    assert calls.count("http://127.0.0.1:11434") == 1


def test_environment_builder_keeps_single_worker_compatible():
    pool = from_environment(
        "http://127.0.0.1:11434",
        {"SONDER_OLLAMA_WORKERS": "", "SONDER_ALLOW_REMOTE_OLLAMA": "0"},
    )
    assert pool.enabled is False
    assert pool.origins == ("http://127.0.0.1:11434",)


def test_server_posts_through_the_pool_selected_origin(monkeypatch):
    import server

    class FakePool:
        enabled = True
        origins = ("http://127.0.0.1:11434", "https://worker.example:11434")
        model_hints = []

        def request(self, sender, *, model=None):
            self.model_hints.append(model)
            return sender("https://worker.example:11434")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ok": true}'

    seen = []

    def open_url(request, timeout=0):
        seen.append((request.full_url, timeout))
        return Response()

    monkeypatch.setattr(server, "OLLAMA_POOL", FakePool())
    monkeypatch.setattr(server.ollama_endpoint, "open_url", open_url)

    assert server._post("/api/chat", {"model": "sonder:latest"}) == {"ok": True}
    assert seen[0][0] == "https://worker.example:11434/api/chat"
    assert FakePool.model_hints == ["sonder:latest"]


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


PRIMARY = "http://127.0.0.1:11434"
SECOND = "http://127.0.0.2:11434"


def test_pool_routes_to_the_least_inflight_worker():
    pool = OllamaWorkerPool(PRIMARY, (SECOND,))
    inner_origins = []

    def inner(origin):
        inner_origins.append(origin)
        return origin

    def outer(origin):
        # While this request is inflight on `origin`, a concurrent request
        # must route to the idle worker rather than queueing behind it.
        pool.request(inner)
        return origin

    first = pool.request(outer)
    assert inner_origins == [origin for origin in pool.origins if origin != first]


def test_pool_breaks_idle_ties_toward_the_lower_latency_worker():
    clock = FakeClock()
    pool = OllamaWorkerPool(PRIMARY, (SECOND,), time_fn=clock)
    durations = {PRIMARY: 2.0, SECOND: 0.5}
    chosen = []

    def send(origin):
        chosen.append(origin)
        clock.advance(durations[origin])
        return origin

    # Unmeasured workers sort first, so the first two requests measure both.
    pool.request(send)
    pool.request(send)
    assert set(chosen) == {PRIMARY, SECOND}
    # With both idle and measured, the faster host wins the tie.
    pool.request(send)
    pool.request(send)
    assert chosen[2:] == [SECOND, SECOND]

    status = {item["origin"]: item for item in pool.status()["workers"]}
    assert status[PRIMARY]["ewma_latency_ms"] == 2000.0
    assert status[SECOND]["ewma_latency_ms"] > 0


def test_circuit_cooldown_backs_off_exponentially_up_to_a_cap():
    clock = FakeClock()
    pool = OllamaWorkerPool(
        PRIMARY, (SECOND,), failure_threshold=1, cooldown_seconds=30, time_fn=clock,
    )

    def send(origin):
        if origin == PRIMARY:
            raise URLError("primary down")
        clock.advance(0.25)
        return origin

    def primary_snapshot():
        return next(s for s in pool.snapshots() if s.origin == PRIMARY)

    pool.request(send)
    snap = primary_snapshot()
    assert snap.trips == 1
    assert snap.cooldown_until == pytest.approx(clock.now + 30, abs=1)

    clock.advance(31)
    pool.request(send)  # the half-open trial fails: the cooldown doubles
    snap = primary_snapshot()
    assert snap.trips == 2
    assert snap.cooldown_until == pytest.approx(clock.now + 60, abs=1)

    for _ in range(6):
        clock.advance(1000)
        pool.request(send)
    snap = primary_snapshot()
    # The backoff is capped at 8x the base cooldown.
    assert snap.cooldown_until - clock.now == pytest.approx(240, abs=1)


def test_half_open_worker_admits_one_trial_and_recovers_on_success():
    clock = FakeClock()
    pool = OllamaWorkerPool(
        PRIMARY, (SECOND,), failure_threshold=1, cooldown_seconds=30, time_fn=clock,
    )

    def fail_primary(origin):
        if origin == PRIMARY:
            raise URLError("primary down")
        clock.advance(0.5)  # gives the healthy worker a measured latency
        return origin

    pool.request(fail_primary)  # trips the primary circuit
    clock.advance(31)

    concurrent = []

    def trial(origin):
        if origin == PRIMARY:
            # While the half-open trial is inflight, a concurrent request must
            # not also be admitted to the probing worker.
            concurrent.append(pool.request(lambda o: o))
        return origin

    # The unmeasured, idle primary ranks first, so it receives the trial.
    assert pool.request(trial) == PRIMARY
    assert concurrent == [SECOND]

    snap = next(s for s in pool.snapshots() if s.origin == PRIMARY)
    assert snap.healthy is True
    assert snap.trips == 0
    assert snap.probing is False


def test_model_affinity_orders_workers_lacking_the_model_last():
    clock = FakeClock()
    pool = OllamaWorkerPool(PRIMARY, (SECOND,), time_fn=clock)
    assert pool.note_models("127.0.0.1:11434", ["llama3:latest"]) is True
    assert pool.note_models(SECOND, ["qwen3-coder:30b"]) is True
    assert pool.note_models("nonexistent:1", ["x"]) is False

    chosen = []

    def send(origin):
        chosen.append(origin)
        return origin

    pool.request(send, model="qwen3-coder:30b")
    # Requesting a model only the primary advertises matches ":latest"
    # normalization and outranks inflight/latency/rotation ties.
    pool.request(send, model="llama3")
    pool.request(send, model="LLAMA3:latest")
    assert chosen == [SECOND, PRIMARY, PRIMARY]

    # A worker with recorded inventory is deprioritized but never excluded.
    failed = []

    def send_failing_second(origin):
        if origin == SECOND:
            failed.append(origin)
            raise URLError("second down")
        return origin

    assert pool.request(send_failing_second, model="qwen3-coder:30b") == PRIMARY
    assert failed == [SECOND]


def test_pool_never_replays_a_classified_post_response_failure():
    """ModelCallError subclasses URLError, but it means a worker answered and
    the response was judged (oversized body, malformed envelope). Replaying it
    on another host would violate the no-replay contract."""
    from sonder_runtime.adapters.model_transport import ModelCallError

    pool = OllamaWorkerPool(PRIMARY, (SECOND,))
    calls = []

    def send(origin):
        calls.append(origin)
        raise ModelCallError("protocol", "response exceeded the safety limit")

    with pytest.raises(ModelCallError):
        pool.request(send)
    assert len(calls) == 1

    # An upstream 502/503/504 produced no model response, so it still fails over.
    unavailable = []

    def send_unavailable(origin):
        unavailable.append(origin)
        if origin == unavailable[0]:
            raise ModelCallError("http", "worker overloaded", status=503)
        return origin

    assert pool.request(send_unavailable) not in (None, unavailable[0])
    assert len(unavailable) == 2


def test_refresh_inventory_records_models_and_keeps_stale_records_on_error():
    pool = OllamaWorkerPool(PRIMARY, (SECOND,))
    payloads = {
        PRIMARY: {"models": [{"name": "llama3:latest"}, {"model": "sonder:latest"}, None, {}]},
        SECOND: {"models": [{"name": "qwen3-coder:30b"}]},
    }

    results = pool.refresh_inventory(lambda origin: payloads[origin])
    assert results == {"127.0.0.1:11434": 2, "127.0.0.2:11434": 1}

    chosen = []
    pool.request(lambda origin: chosen.append(origin) or origin, model="llama3")
    assert chosen == [PRIMARY]

    # A failed probe reports the error and keeps the previous inventory.
    def failing_fetch(origin):
        if origin == PRIMARY:
            raise OSError("unreachable")
        return {"models": []}

    results = pool.refresh_inventory(failing_fetch)
    assert results["127.0.0.1:11434"].startswith("error:")
    assert results["127.0.0.2:11434"] == 0
    chosen.clear()
    pool.request(lambda origin: chosen.append(origin) or origin, model="llama3")
    assert chosen == [PRIMARY]

    # A malformed envelope is an error, never an empty catalog.
    results = pool.refresh_inventory(lambda origin: {"models": "garbage"})
    assert all(str(value).startswith("error:") for value in results.values())


def test_worker_origin_rejects_inline_credentials_and_missing_port():
    with pytest.raises(ValueError, match="inline credentials"):
        validate_worker_origin("https://user:pw@worker.example:11434", allow_remote=True)
    with pytest.raises(ValueError, match="explicit port"):
        validate_worker_origin("https://worker.example", allow_remote=True)


def test_environment_builder_accepts_consented_https_remote_workers():
    pool = from_environment(
        PRIMARY,
        {
            "SONDER_OLLAMA_WORKERS": "https://ollama-pc2.example:443",
            "SONDER_ALLOW_REMOTE_OLLAMA": "1",
        },
    )
    assert pool.enabled is True
    assert pool.has_remote_workers is True
    assert pool.status()["remote_worker_count"] == 1
