from __future__ import annotations

from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    _metric_label,
    configure_typed_workers,
    from_environment,
    parse_worker_origins,
    reset_typed_workers,
    validate_worker_origin,
)
from sonder_runtime.platform.logging import Redactor


class _Metrics:
    def __init__(self):
        self.requests = []
        self.circuits = []

    def observe_ollama_worker_request(self, *, worker, result, elapsed_seconds):
        self.requests.append((worker, result))

    def observe_ollama_worker_circuit(self, *, worker, state):
        self.circuits.append((worker, state))


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


@pytest.mark.parametrize("origin, message", [
    ("ftp://127.0.0.1:11434", "http or https"),
    ("http://127.0.0.1:11434/api", "without a path"),
    ("http://127.0.0.1:11434?token=secret", "without a path"),
    ("http://127.0.0.1:11434/#fragment", "without a path"),
])
def test_worker_origin_rejects_non_origin_and_ambiguous_url_syntax(origin, message):
    with pytest.raises(ValueError, match=message):
        validate_worker_origin(origin, allow_remote=False)


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

    result = pool.request(send, idempotent=True)

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


def test_pool_never_replays_ambiguous_non_idempotent_transport_failure():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434", ("http://127.0.0.2:11434",)
    )
    calls = []

    def send(origin):
        calls.append(origin)
        raise URLError("connection disappeared after request send")

    with pytest.raises(URLError):
        pool.request(send)
    assert calls == ["http://127.0.0.1:11434"]


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

    pool.request(send, idempotent=True)
    pool.request(send, idempotent=True)
    status = pool.status()
    primary = next(item for item in status["workers"] if item["origin"].endswith(".1:11434"))
    assert primary["healthy"] is False
    assert primary["consecutive_failures"] == 1
    assert calls.count("http://127.0.0.1:11434") == 1


def test_environment_builder_keeps_single_worker_compatible():
    reset_typed_workers()
    pool = from_environment(
        "http://127.0.0.1:11434",
        {"SONDER_OLLAMA_WORKERS": "", "SONDER_ALLOW_REMOTE_OLLAMA": "0"},
    )
    assert pool.enabled is False
    assert pool.origins == ("http://127.0.0.1:11434",)


def test_typed_workers_are_authoritative_without_environment_round_trip(monkeypatch):
    try:
        configure_typed_workers(
            ("https://worker.example:443",), allow_remote=True,
        )
        monkeypatch.setenv("SONDER_OLLAMA_WORKERS", "")
        monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "0")
        pool = from_environment("http://127.0.0.1:11434")
        assert pool.origins == (
            "http://127.0.0.1:11434", "https://worker.example:443",
        )
        assert pool.status()["tls_verification"] == "system-trust-store"
        assert pool.status()["non_idempotent_failover"] is False
    finally:
        reset_typed_workers()


def test_explicit_environment_remains_an_injectable_compatibility_boundary():
    try:
        configure_typed_workers((), allow_remote=False)
        pool = from_environment(
            "http://127.0.0.1:11434",
            {"SONDER_OLLAMA_WORKERS": "http://127.0.0.2:11434"},
        )
        assert pool.origins == (
            "http://127.0.0.1:11434", "http://127.0.0.2:11434",
        )
    finally:
        reset_typed_workers()


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
    pool.request(send, idempotent=True)
    pool.request(send, idempotent=True)
    assert set(chosen) == {PRIMARY, SECOND}
    # With both idle and measured, the faster host wins the tie.
    pool.request(send, idempotent=True)
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

    pool.request(send, idempotent=True)
    snap = primary_snapshot()
    assert snap.trips == 1
    assert snap.cooldown_until == pytest.approx(clock.now + 30, abs=1)

    clock.advance(31)
    pool.request(send, idempotent=True)  # the half-open trial fails: the cooldown doubles
    snap = primary_snapshot()
    assert snap.trips == 2
    assert snap.cooldown_until == pytest.approx(clock.now + 60, abs=1)

    for _ in range(6):
        clock.advance(1000)
        pool.request(send, idempotent=True)
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

    pool.request(fail_primary, idempotent=True)  # trips the primary circuit
    clock.advance(31)

    concurrent = []

    def trial(origin):
        if origin == PRIMARY:
            # While the half-open trial is inflight, a concurrent request must
            # not also be admitted to the probing worker.
            concurrent.append(pool.request(lambda o: o))
        return origin

    # The unmeasured, idle primary ranks first, so it receives the trial.
    assert pool.request(trial, idempotent=True) == PRIMARY
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

    assert pool.request(
        send_failing_second, model="qwen3-coder:30b", idempotent=True,
    ) == PRIMARY
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

    assert pool.request(send_unavailable, idempotent=True) not in (None, unavailable[0])
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
def test_metric_label_is_a_bounded_ordinal_never_a_raw_hostname():
    assert _metric_label(0) == "w0"
    assert _metric_label(15) == "w15"
    assert _metric_label(16) == "overflow"
    assert _metric_label(500) == "overflow"


def test_pool_records_request_metrics_by_bounded_worker_slot_on_success_and_failure():
    metrics = _Metrics()
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("https://worker-a.example:11434",),
        allow_remote=True,
        metrics=metrics,
    )

    def send(origin):
        if origin == "http://127.0.0.1:11434":
            raise URLError("primary unavailable")
        return "ok"

    pool.request(send, idempotent=True)

    assert metrics.requests == [("w0", "error"), ("w1", "ok")]


def test_pool_emits_one_circuit_open_transition_and_closes_on_recovery():
    # Exercises _finish's breaker bookkeeping directly rather than through
    # request(), because the least-inflight scheduler stops offering an
    # already-cooling worker to new requests -- so a second consecutive
    # transport failure on the *same* worker cannot be forced deterministically
    # through the public request() path once one other healthy worker exists.
    metrics = _Metrics()
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("http://127.0.0.2:11434",),
        failure_threshold=2,
        cooldown_seconds=60,
        metrics=metrics,
    )
    state = pool._states[0]
    error = URLError("down")

    pool._finish(state, error, 0.01)
    assert metrics.circuits == []

    pool._finish(state, error, 0.01)
    assert metrics.circuits.count(("w0", "open")) == 1

    # A further failure while the breaker is already open must not re-emit
    # the "open" transition.
    pool._finish(state, error, 0.01)
    assert metrics.circuits.count(("w0", "open")) == 1

    pool._finish(state, None, 0.01)
    assert metrics.circuits.count(("w0", "closed")) == 1


def test_pool_redacts_secret_shaped_text_out_of_stored_last_error():
    pool = OllamaWorkerPool(
        "http://127.0.0.1:11434",
        ("http://127.0.0.2:11434",),
        redactor=Redactor(),
    )

    def send(origin):
        if origin == "http://127.0.0.1:11434":
            raise ConnectionError("connect failed: api_key=sk-abcdef0123456789 rejected")
        return origin

    pool.request(send, idempotent=True)

    primary = next(s for s in pool.snapshots() if s.origin.endswith(".1:11434"))
    assert "sk-abcdef0123456789" not in primary.last_error
    assert "[REDACTED]" in primary.last_error
def test_local_only_never_reaches_the_pool_even_with_remote_workers(monkeypatch):
    """A caller-declared local_only contract (vision bytes, fanout receipts)

    must stay pinned to the primary endpoint regardless of pool state -- the
    least-inflight scheduler is never given a chance to pick a remote worker.
    """
    import server

    class FakePool:
        enabled = True
        has_remote_workers = True

        def request(self, _sender):
            pytest.fail("local_only requests must bypass the worker pool entirely")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"ok": true}'

    seen = []

    def open_url(request, timeout=0):
        seen.append(request.full_url)
        return Response()

    monkeypatch.setattr(server, "OLLAMA_POOL", FakePool())
    monkeypatch.setattr(server.ollama_endpoint, "open_url", open_url)

    assert server._post("/api/chat", {}, local_only=True) == {"ok": True}
    assert seen == [server.BASE + "/api/chat"]


def test_post_model_local_only_rejects_a_non_loopback_primary(monkeypatch):
    """local_only is a hard refusal, not a silent downgrade to the primary."""
    import server

    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: False)

    with pytest.raises(server.ModelCallError, match="loopback Ollama endpoint"):
        server._post_model(
            "/api/chat", {}, model="local-model", local_only=True,
        )


def test_post_model_rejects_local_only_combined_with_cloud():
    import server

    with pytest.raises(ValueError, match="mutually exclusive"):
        server._post_model(
            "/api/chat", {}, model="local-model", cloud=True, local_only=True,
        )


def test_endpoint_locality_reflects_a_configured_remote_worker(monkeypatch):
    """Locality displays/caching must not call a loopback primary "local" once

    a remote worker is configured -- the pool can route any ordinary request
    there even though BASE itself never changed.
    """
    import server

    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)

    class LocalOnlyPool:
        has_remote_workers = False

    class RemoteCapablePool:
        has_remote_workers = True

    monkeypatch.setattr(server, "OLLAMA_POOL", LocalOnlyPool())
    assert server._ollama_endpoint_is_local() is True

    monkeypatch.setattr(server, "OLLAMA_POOL", RemoteCapablePool())
    assert server._ollama_endpoint_is_local() is False
