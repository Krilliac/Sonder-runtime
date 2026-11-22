"""Approved private Sonder Inference workers: consent, placement, failover.

``SONDER_INFERENCE_PRIVATE_WORKERS`` lists operator-approved private
endpoints (https, private IP literal, own CA bundle, own token variable).  It
is honoured only with ``SONDER_ALLOW_REMOTE_INFERENCE=1``, never needs or
grants cloud consent, and places each request whole on the least-loaded
endpoint.  Everything runs on injected transport seams (no network).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters import tls_contexts
from sonder_runtime.adapters.inference import sonder_inference_gateway as gateway_module
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    ENV_PRIVATE_WORKERS,
    SonderInferenceGateway,
    config_from_env,
    parse_private_workers,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
)

PRIMARY = "http://127.0.0.1:11437"
WORKER = "https://10.77.0.2:8443/sonder-inference"
TOKEN = "t" * 32
PRIMARY_KEY = "p" * 32


def _health(models=("sonder:latest",), status="ready"):
    return {
        "status": status, "api_version": 1, "version": "0.1.0", "commit": "c",
        "instance_id": "i", "synthetic": False,
        "models": [{"id": m, "backend": "ollama", "default": i == 0} for i, m in enumerate(models)],
    }


def _chat(model="sonder:latest"):
    return {
        "id": "c", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class Hosts:
    """GET/POST seams keyed by base URL; records URL, headers and CA scope."""

    def __init__(self, *, health=None, chat=None):
        self.health = {PRIMARY: _health(), WORKER: _health()}
        self.health.update(health or {})
        self.chat = chat or {}
        self.gets: list[tuple[str, dict, str]] = []
        self.posts: list[tuple[str, dict, str]] = []
        self.lock = threading.Lock()

    @staticmethod
    def _base(url):
        return WORKER if url.startswith(WORKER) else PRIMARY

    def get(self, url, headers, timeout):
        base = self._base(url)
        with self.lock:
            self.gets.append((base, dict(headers), getattr(gateway_module._TRUST, "ca_bundle", "")))
        document = self.health[base]
        if isinstance(document, BaseException):
            raise document
        return (200 if document.get("status") == "ready" else 503), json.dumps(document).encode()

    def post(self, url, payload, headers, timeout):
        base = self._base(url)
        with self.lock:
            self.posts.append((base, dict(headers), getattr(gateway_module._TRUST, "ca_bundle", "")))
        behaviour = self.chat.get(base)
        if callable(behaviour):
            behaviour = behaviour(payload)
        if isinstance(behaviour, BaseException):
            raise behaviour
        served = payload["model"] if payload["model"] != "default" else "sonder:latest"
        return behaviour or _chat(served)


@pytest.fixture
def ca_bundle(tmp_path):
    path = tmp_path / "private-ca.pem"
    path.write_text("-----BEGIN CERTIFICATE-----\n-----END CERTIFICATE-----\n")
    return str(path)


def _workers(ca_bundle, **extra):
    return json.dumps([{"url": WORKER, "ca_bundle": ca_bundle,
                        "token_env": "SONDER_INFERENCE_NODE1_TOKEN", **extra}])


def _env(ca_bundle, **overrides):
    env = {
        "SONDER_INFERENCE_BASE_URL": PRIMARY,
        "SONDER_INFERENCE_MODEL": "sonder:latest",
        "SONDER_ALLOW_REMOTE_INFERENCE": "1",
        ENV_PRIVATE_WORKERS: _workers(ca_bundle),
        "SONDER_INFERENCE_NODE1_TOKEN": TOKEN,
    }
    env.update(overrides)
    return {key: value for key, value in env.items() if value is not None}


def _gateway(hosts, env):
    return SonderInferenceGateway(
        transport=hosts.post, get_transport=hosts.get, env=env,
        wall_clock=lambda: datetime(2026, 10, 8, tzinfo=timezone.utc),
    )


def _ctx(cloud=False):
    return local_owner_context(correlation_id="turn-1", source="mcp",
                               cloud_allowed=cloud, timeout_seconds=30.0)


# -- configuration: the consent lane ----------------------------------------


def test_private_workers_require_the_remote_inference_opt_in(ca_bundle):
    with pytest.raises(InvalidInput, match="SONDER_ALLOW_REMOTE_INFERENCE"):
        config_from_env(_env(ca_bundle, SONDER_ALLOW_REMOTE_INFERENCE=None))
    with pytest.raises(InvalidInput, match="SONDER_ALLOW_REMOTE_INFERENCE"):
        config_from_env(_env(ca_bundle, SONDER_ALLOW_REMOTE_INFERENCE="0"))


def test_no_worker_list_means_no_workers(ca_bundle):
    settings = config_from_env(_env(ca_bundle, **{ENV_PRIVATE_WORKERS: None}))
    assert settings.workers == ()


@pytest.mark.parametrize("url, reason", [
    ("http://10.77.0.2:8443", "https"),
    ("https://8.8.8.8:443", "not a private-network address"),
    ("https://node1.example.com:8443", "IP literal"),
    ("https://127.0.0.1:8443", "not a private-network address"),
    ("https://10.77.0.2", "explicit port"),
    ("https://user:pw@10.77.0.2:8443", "without credentials"),
])
def test_worker_urls_are_private_https_ip_literals(ca_bundle, url, reason):
    raw = json.dumps([{"url": url, "ca_bundle": ca_bundle, "token_env": "NODE1_TOKEN"}])
    with pytest.raises(InvalidInput, match=reason):
        parse_private_workers(raw, allow_remote=True)


def test_worker_entries_are_validated(ca_bundle, tmp_path):
    def parse(**entry):
        base = {"url": WORKER, "ca_bundle": ca_bundle, "token_env": "NODE1_TOKEN"}
        base.update(entry)
        return parse_private_workers(json.dumps([{k: v for k, v in base.items() if v is not None}]),
                                     allow_remote=True)

    assert parse()[0].url == WORKER
    with pytest.raises(InvalidInput, match="ca_bundle"):
        parse(ca_bundle=str(tmp_path / "missing.pem"))
    with pytest.raises(InvalidInput, match="ca_bundle"):
        parse(ca_bundle="relative.pem")
    with pytest.raises(InvalidInput, match="object with url"):
        parse(ca_bundle=None)
    with pytest.raises(InvalidInput, match="token_env"):
        parse(token_env="lower-case")
    with pytest.raises(InvalidInput, match="must not reuse"):
        parse(token_env="SONDER_INFERENCE_API_KEY")
    with pytest.raises(InvalidInput, match="max_inflight"):
        parse(max_inflight=0)
    with pytest.raises(InvalidInput, match="object with url"):
        parse(api_key="inline-secret-is-never-accepted")
    with pytest.raises(InvalidInput, match="JSON array"):
        parse_private_workers("{not json", allow_remote=True)
    two = json.dumps([{"url": WORKER, "ca_bundle": ca_bundle, "token_env": "A_TOKEN"}] * 2)
    with pytest.raises(InvalidInput, match="duplicates"):
        parse_private_workers(two, allow_remote=True)
    shared_token = json.dumps([
        {"url": WORKER, "ca_bundle": ca_bundle, "token_env": "A_TOKEN"},
        {"url": "https://10.77.0.3:8443", "ca_bundle": ca_bundle, "token_env": "A_TOKEN"},
    ])
    with pytest.raises(InvalidInput, match="token_env duplicates"):
        parse_private_workers(shared_token, allow_remote=True)
    many = json.dumps([{"url": "https://10.77.0.%d:8443" % i, "ca_bundle": ca_bundle,
                        "token_env": "A_TOKEN"} for i in range(2, 11)])
    with pytest.raises(InvalidInput, match="1-8"):
        parse_private_workers(many, allow_remote=True)


# -- consent at call time ----------------------------------------------------


def test_worker_serves_without_cloud_consent_and_never_needs_it(ca_bundle):
    hosts = Hosts(chat={PRIMARY: DependencyUnavailable("unused")})
    hosts.health[PRIMARY] = _health(status="starting")  # primary not ready
    gateway = _gateway(hosts, _env(ca_bundle))
    response = gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx(cloud=False))
    assert response.text == "ok"
    assert [base for base, _h, _ca in hosts.posts] == [WORKER]
    assert response.endpoint == "https://10.77.0.2:8443"


def test_unlisted_remote_endpoint_still_needs_cloud_consent(ca_bundle):
    """The worker lane does not relax the primary's own remote rules."""
    hosts = Hosts()
    env = _env(ca_bundle, SONDER_INFERENCE_BASE_URL="https://10.77.0.9:8443",
               SONDER_INFERENCE_API_KEY=PRIMARY_KEY, **{ENV_PRIVATE_WORKERS: None})
    with pytest.raises(Forbidden, match="does not allow prompts to leave"):
        _gateway(hosts, env).generate(ModelRequest(prompt="x", tier="fast"), _ctx(cloud=False))
    assert hosts.posts == []


def test_each_endpoint_gets_only_its_own_credentials_and_trust(ca_bundle):
    hosts = Hosts()
    env = _env(ca_bundle, SONDER_INFERENCE_API_KEY=PRIMARY_KEY)
    gateway = _gateway(hosts, env)
    gateway.generate(ModelRequest(prompt="a", tier="fast"), _ctx())
    gateway.generate(ModelRequest(prompt="b", tier="fast"), _ctx())
    by_base = {}
    for base, headers, ca in hosts.gets + hosts.posts:
        by_base.setdefault(base, set()).add((headers.get("Authorization"), ca))
    assert by_base[WORKER] == {("Bearer " + TOKEN, ca_bundle)}
    assert by_base[PRIMARY] == {("Bearer " + PRIMARY_KEY, "")}


def test_worker_without_its_token_is_skipped_and_reported(ca_bundle):
    hosts = Hosts()
    gateway = _gateway(hosts, _env(ca_bundle, SONDER_INFERENCE_NODE1_TOKEN=None))
    for prompt in ("a", "b", "c"):
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY}
    assert all(base == PRIMARY for base, _h, _ca in hosts.gets)
    row = gateway.provider_status()["sonder_inference"]["workers"][0]
    assert row["healthy"] is False and "SONDER_INFERENCE_NODE1_TOKEN" in row["detail"]
    assert TOKEN not in json.dumps(gateway.provider_status())


# -- placement -----------------------------------------------------------------


def test_concurrent_requests_spread_across_primary_and_worker(ca_bundle):
    barrier = threading.Barrier(2, timeout=10)

    def wait_for_peer(payload):
        barrier.wait()  # both requests are in flight at the same time
        return _chat(payload["model"])

    hosts = Hosts(chat={PRIMARY: wait_for_peer, WORKER: wait_for_peer})
    gateway = _gateway(hosts, _env(ca_bundle))
    results, errors = [], []

    def run(prompt):
        try:
            results.append(gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx()))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(p,)) for p in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert sorted(base for base, _h, _ca in hosts.posts) == sorted([PRIMARY, WORKER])
    assert {r.endpoint for r in results} == {PRIMARY, "https://10.77.0.2:8443"}
    assert gateway._inflight == {}


def test_sequential_requests_use_both_idle_endpoints(ca_bundle):
    # Which endpoint gets the later calls depends on measured latency (real
    # wall time here), so only the exploration of both is deterministic.
    hosts = Hosts()
    gateway = _gateway(hosts, _env(ca_bundle))
    for prompt in "abcd":
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY, WORKER}


def test_a_much_slower_worker_only_gets_overflow(ca_bundle):
    import time as _time

    def slow(payload):
        _time.sleep(0.25)
        return _chat(payload["model"])

    hosts = Hosts(chat={WORKER: slow})
    gateway = _gateway(hosts, _env(ca_bundle))
    for prompt in "ab":  # one each: both endpoints get observed
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert sorted(base for base, _h, _ca in hosts.posts) == sorted([PRIMARY, WORKER])
    hosts.posts.clear()
    for prompt in "cdef":
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY}
    assert gateway._ms_per_token[WORKER] > gateway._ms_per_token[PRIMARY]


def test_every_endpoint_is_measured_before_capacity_weights_apply(ca_bundle):
    """A worker with more slots must not starve the unmeasured primary."""
    hosts = Hosts()
    gateway = _gateway(hosts, _env(ca_bundle, **{ENV_PRIVATE_WORKERS: _workers(ca_bundle, max_inflight=4)}))
    for prompt in "ab":
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert sorted(base for base, _h, _ca in hosts.posts) == sorted([PRIMARY, WORKER])


def test_primary_capacity_is_configurable(ca_bundle):
    assert config_from_env(_env(ca_bundle, SONDER_INFERENCE_MAX_INFLIGHT="8")).max_inflight == 8
    with pytest.raises(InvalidInput, match="SONDER_INFERENCE_MAX_INFLIGHT"):
        config_from_env(_env(ca_bundle, SONDER_INFERENCE_MAX_INFLIGHT="0"))


def test_unreachable_worker_falls_back_to_primary(ca_bundle):
    hosts = Hosts()
    hosts.health[WORKER] = ConnectionRefusedError()
    gateway = _gateway(hosts, _env(ca_bundle))
    for prompt in "abc":
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY}


def test_executed_failure_is_never_replayed_elsewhere(ca_bundle):
    from urllib.error import HTTPError
    import io

    def boom(payload):
        return HTTPError(WORKER, 500, "x", {}, io.BytesIO(b'{"error":{"code":"internal"}}'))

    hosts = Hosts(chat={WORKER: boom})
    hosts.health[PRIMARY] = _health(status="starting")
    gateway = _gateway(hosts, _env(ca_bundle))
    with pytest.raises(DependencyUnavailable):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert [base for base, _h, _ca in hosts.posts] == [WORKER]


def test_worker_without_the_model_is_not_chosen(ca_bundle):
    hosts = Hosts()
    hosts.health[WORKER] = _health(models=("sonder:latest",))
    hosts.health[PRIMARY] = _health(models=("sonder:latest", "big-coder"))
    env = _env(ca_bundle, SONDER_INFERENCE_TIER_MODELS="code=big-coder")
    gateway = _gateway(hosts, env)
    gateway.provider_status()  # warms both health caches
    for prompt in "abc":
        gateway.generate(ModelRequest(prompt=prompt, tier="code"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY}


def test_cold_worker_is_never_sent_a_model_it_does_not_list(ca_bundle):
    """No warm-up: the first placements must already respect the worker's models."""
    hosts = Hosts()
    hosts.health[WORKER] = _health(models=("qwen3.6:35b", "sonder:latest"))
    hosts.health[PRIMARY] = _health(models=("sonder:latest", "big-coder"))
    gateway = _gateway(hosts, _env(ca_bundle, SONDER_INFERENCE_TIER_MODELS="code=big-coder"))
    for prompt in "abcdef":
        gateway.generate(ModelRequest(prompt=prompt, tier="code"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY}
    hosts.posts.clear()
    gateway.generate(ModelRequest(prompt="x", tier="fast", options={"model": "qwen3.6:35b"}), _ctx())
    assert [base for base, _h, _ca in hosts.posts] == [WORKER]


def test_model_only_the_worker_serves_never_goes_to_the_primary_first(ca_bundle):
    hosts = Hosts()
    hosts.health[WORKER] = _health(models=("sonder:latest", "qwen3.6:35b"))
    gateway = _gateway(hosts, _env(ca_bundle))
    for prompt in "abc":
        gateway.generate(ModelRequest(prompt=prompt, tier="general", options={"model": "qwen3.6:35b"}), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {WORKER}


def test_default_alias_never_selects_a_worker(ca_bundle):
    hosts = Hosts()
    gateway = _gateway(hosts, _env(ca_bundle, SONDER_INFERENCE_MODEL=None))
    for prompt in "abcd":
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY}


def test_worker_whose_health_cannot_be_read_gets_nothing(ca_bundle):
    hosts = Hosts()
    hosts.health[WORKER] = _health(status="starting")
    gateway = _gateway(hosts, _env(ca_bundle))
    for prompt in "abc":
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx())
    assert {base for base, _h, _ca in hosts.posts} == {PRIMARY}


def test_remote_primary_without_cloud_consent_is_not_a_candidate(ca_bundle):
    """Only the approved worker may take a no-cloud call; never a final Forbidden."""
    hosts = Hosts()
    remote_primary = "https://10.77.0.9:8443"
    hosts._base = staticmethod(lambda url: WORKER if url.startswith(WORKER) else PRIMARY)
    env = _env(ca_bundle, SONDER_INFERENCE_BASE_URL=remote_primary,
               SONDER_INFERENCE_API_KEY=PRIMARY_KEY)
    gateway = _gateway(hosts, env)
    for prompt in "abcd":
        response = gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx(cloud=False))
        assert response.endpoint == "https://10.77.0.2:8443"
    assert {base for base, _h, _ca in hosts.posts} == {WORKER}
    # With cloud consent the remote primary is eligible again.
    hosts.posts.clear()
    for prompt in "efgh":
        gateway.generate(ModelRequest(prompt=prompt, tier="fast"), _ctx(cloud=True))
    assert PRIMARY in {base for base, _h, _ca in hosts.posts}


def test_status_lists_workers_without_secrets(ca_bundle):
    hosts = Hosts()
    status = _gateway(hosts, _env(ca_bundle)).provider_status()["sonder_inference"]
    assert status["workers"] == [{
        "base_url": "https://10.77.0.2:8443", "state": "ready", "healthy": True,
        "detail": "ready: 1 model(s)", "models": ["sonder:latest"], "max_inflight": 1,
        "inflight": 0, "ms_per_token": None,
    }]
    assert TOKEN not in json.dumps(status) and "sonder-inference" not in status["workers"][0]["base_url"]


def test_trust_scope_pins_the_worker_bundle(ca_bundle, monkeypatch):
    seen = []
    real = gateway_module.ssl.create_default_context

    def record(*args, **kwargs):
        seen.append(kwargs.get("cafile"))
        return real()

    tls_contexts.reset_https_contexts()
    monkeypatch.setattr(gateway_module.ssl, "create_default_context", record)
    try:
        with gateway_module.trust_scope(ca_bundle):
            pinned = gateway_module._tls_context()
            with gateway_module.trust_scope(""):
                system = gateway_module._tls_context()
            assert gateway_module._tls_context() is not pinned
        assert gateway_module._tls_context() is system
        assert system is gateway_module.shared_https_context()
        assert system is not pinned
        # The pinned bundle is built per call inside the scope; the system
        # context is built once and reused.
        assert seen == [ca_bundle, None, ca_bundle]
    finally:
        tls_contexts.reset_https_contexts()


# -- inflight slots: queueing, saturation and cancellation -------------------


def test_host_queue_enforces_per_endpoint_max_inflight_across_16_callers(ca_bundle):
    """16 concurrent callers queue on the host without exceeding any endpoint's max_inflight."""
    import time as _time

    active = {PRIMARY: 0, WORKER: 0}
    peaks = {PRIMARY: 0, WORKER: 0}
    counter_lock = threading.Lock()

    def make_handler(base):
        def handle(payload):
            with counter_lock:
                active[base] += 1
                peaks[base] = max(peaks[base], active[base])
            _time.sleep(0.03)
            with counter_lock:
                active[base] -= 1
            return _chat(payload["model"])
        return handle

    hosts = Hosts(chat={PRIMARY: make_handler(PRIMARY), WORKER: make_handler(WORKER)})
    gateway = _gateway(hosts, _env(ca_bundle))  # primary=1, worker=1
    results, errors = [], []

    def run(i):
        try:
            results.append(gateway.generate(ModelRequest(prompt=f"p{i}", tier="fast"), _ctx()))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)

    assert errors == []
    assert len(results) == 16
    assert peaks[PRIMARY] <= 1 and peaks[WORKER] <= 1
    assert len(hosts.posts) == 16
    assert gateway._inflight == {}


def test_host_queue_honors_higher_worker_max_inflight(ca_bundle):
    import time as _time

    active = {PRIMARY: 0, WORKER: 0}
    peaks = {PRIMARY: 0, WORKER: 0}
    counter_lock = threading.Lock()

    def make_handler(base):
        def handle(payload):
            with counter_lock:
                active[base] += 1
                peaks[base] = max(peaks[base], active[base])
            _time.sleep(0.04)
            with counter_lock:
                active[base] -= 1
            return _chat(payload["model"])
        return handle

    hosts = Hosts(chat={PRIMARY: make_handler(PRIMARY), WORKER: make_handler(WORKER)})
    gateway = _gateway(hosts, _env(ca_bundle, **{ENV_PRIVATE_WORKERS: _workers(ca_bundle, max_inflight=2)}))
    errors = []

    def run(i):
        try:
            gateway.generate(ModelRequest(prompt=f"p{i}", tier="fast"), _ctx())
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert errors == []
    assert peaks[PRIMARY] <= 1
    assert peaks[WORKER] == 2
    assert len(hosts.posts) == 8
    assert gateway._inflight == {}


def test_overload_does_not_fail_over_onto_a_saturated_endpoint(ca_bundle):
    """With the other endpoint full, a 503 overloaded is raised, not queued behind it."""
    import io
    from urllib.error import HTTPError

    worker_entered = threading.Event()
    release_worker = threading.Event()

    def block_worker(payload):
        worker_entered.set()
        release_worker.wait(5)
        return _chat(payload["model"])

    def overloaded_primary(payload):
        return HTTPError(PRIMARY, 503, "overloaded", {}, io.BytesIO(b'{"error":{"code":"overloaded"}}'))

    hosts = Hosts(chat={WORKER: block_worker, PRIMARY: overloaded_primary})
    gateway = _gateway(hosts, _env(ca_bundle))
    gateway.provider_status()  # warm both health caches

    first_done = []
    t1 = threading.Thread(
        target=lambda: first_done.append(
            gateway.generate(ModelRequest(prompt="occupy", tier="fast"), _ctx())
        )
    )
    t1.start()
    try:
        assert worker_entered.wait(5)
        with pytest.raises(CapacityExceeded):
            gateway.generate(ModelRequest(prompt="second", tier="fast"), _ctx())
    finally:
        release_worker.set()
        t1.join(5)
    assert len(first_done) == 1
    assert [b for b, _h, _ca in hosts.posts].count(WORKER) == 1


def test_timed_out_readiness_does_not_fail_over_onto_a_saturated_endpoint(ca_bundle):
    """A probe timeout (maybe just saturation) is raised at once when the peer is full."""
    import time as _time

    worker_entered = threading.Event()
    release_worker = threading.Event()

    def block_worker(payload):
        worker_entered.set()
        release_worker.wait(5)
        return _chat(payload["model"])

    hosts = Hosts(chat={WORKER: block_worker})
    gateway = _gateway(hosts, _env(ca_bundle))
    gateway.provider_status()  # warm both health caches
    settings = gateway.settings()

    def primary_probe_times_out(request, context):
        raise gateway.unreachable_error(settings, "health probe timed out after 5.0s", timed_out=True)

    gateway._generate_here = primary_probe_times_out  # the primary only; workers are child gateways
    t1 = threading.Thread(
        target=lambda: gateway.generate(ModelRequest(prompt="occupy", tier="fast"), _ctx()),
    )
    t1.start()
    try:
        assert worker_entered.wait(5)
        started = _time.monotonic()
        with pytest.raises(gateway_module.SonderInferenceUnreachable) as caught:
            gateway.generate(ModelRequest(prompt="second", tier="fast"), _ctx())
        assert caught.value.timed_out is True
        assert _time.monotonic() - started < 2.0  # raised, not queued behind the worker
    finally:
        release_worker.set()
        t1.join(5)
    assert [b for b, _h, _ca in hosts.posts].count(WORKER) == 1


def test_post_read_timeout_does_not_fail_over_onto_peer(ca_bundle):
    from sonder_runtime.domain.common.errors import DeadlineExceeded

    hosts = Hosts(chat={PRIMARY: TimeoutError("read timed out"), WORKER: TimeoutError("read timed out")})
    gateway = _gateway(hosts, _env(ca_bundle))
    with pytest.raises(DeadlineExceeded):
        gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert len(hosts.posts) == 1


def test_overloaded_endpoint_fails_over_when_peer_has_free_capacity(ca_bundle):
    import io
    from urllib.error import HTTPError

    def overloaded(payload):
        return HTTPError(PRIMARY, 503, "overloaded", {}, io.BytesIO(b'{"error":{"code":"overloaded"}}'))

    hosts = Hosts(chat={PRIMARY: overloaded})
    gateway = _gateway(hosts, _env(ca_bundle))
    response = gateway.generate(ModelRequest(prompt="x", tier="fast"), _ctx())
    assert response.text == "ok"
    assert response.endpoint == WORKER.rsplit("/", 1)[0]


def test_cancelled_caller_exits_host_slot_queue_cleanly(ca_bundle):
    entered = threading.Event()
    release = threading.Event()
    cancel_event = threading.Event()

    def hold_slot(payload):
        entered.set()
        release.wait(5)
        return _chat(payload["model"])

    hosts = Hosts(chat={PRIMARY: hold_slot, WORKER: hold_slot})
    gateway = _gateway(hosts, _env(ca_bundle))
    gateway.provider_status()

    holders = [
        threading.Thread(target=lambda idx=i: gateway.generate(ModelRequest(prompt=f"h{idx}", tier="fast"), _ctx()))
        for i in range(2)
    ]
    for h in holders:
        h.start()
    try:
        pause = threading.Event()
        for _ in range(250):
            with gateway._lock:
                if sum(gateway._inflight.values()) == 2:
                    break
            pause.wait(0.02)
        assert sum(gateway._inflight.values()) == 2

        threading.Timer(0.05, cancel_event.set).start()
        with pytest.raises(Cancelled):
            gateway.generate(
                ModelRequest(prompt="queued", tier="fast", options={"cancel_token": cancel_event}),
                _ctx(),
            )
        assert len(hosts.posts) == 2  # the queued caller was never dispatched
    finally:
        release.set()
        for h in holders:
            h.join(5)
    assert gateway._inflight == {}
