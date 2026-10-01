"""Static remote workers leave probation in long-lived surfaces; misses refresh once.

Regression coverage for a live defect: ``serve`` composed a static membership
controller but never started it, so a configured HTTPS worker stayed in
``probation`` forever with ``error_category: none`` and no log line.
"""
from datetime import timedelta
import logging
import ssl
import time
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.ollama_pool import (
    OllamaWorkerPool,
    WorkerCapabilityUnavailable,
)
from sonder_runtime.platform.config import OllamaConfig

from tests.test_inference_membership_controller import (
    LOCAL,
    REMOTE,
    Clock,
    Source,
    controller,
    signed_snapshot,
)


class Monotonic:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def _remote_record(pool):
    return next(worker for worker in pool.status()["workers"] if worker["origin"] == REMOTE)


def _patch_entrypoint(monkeypatch, tmp_path, config, prober):
    # The caller's ``isolated_default_runtime`` fixture swaps the default app runtime.
    import server
    import sonder_runtime.__main__ as entrypoint
    from sonder_runtime.adapters.inference import ollama_pool
    from sonder_runtime.adapters.persistence import migrations, operations_store
    from sonder_runtime.adapters.persistence.sqlite import bridge_migration
    from sonder_runtime.bootstrap import app as bootstrap, legacy_root

    stale_pool = OllamaWorkerPool(config.ollama.url, config.ollama.workers, allow_remote=True,
                                  capability_prober=lambda _: {"models": []})
    monkeypatch.setattr(server, "OLLAMA_POOL", stale_pool)
    monkeypatch.setattr(server, "BASE", config.ollama.url)
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(legacy_root, "_owned_application", None)
    monkeypatch.setattr(entrypoint, "_load_config", lambda _: config)
    monkeypatch.setattr(entrypoint, "_export_runtime_environment", lambda *_a, **_k: None)
    monkeypatch.setattr(bridge_migration, "require_epoch_2", lambda _: None)
    monkeypatch.setattr(migrations, "migrate_all", lambda **_: None)
    monkeypatch.setattr(operations_store, "OperationsStore",
                        lambda: SimpleNamespace(prune_events=lambda _: 0))
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    # Replace only network I/O: the real consent/HTTPS policy still validates origins.
    monkeypatch.setattr(ollama_pool, "_default_capability_prober", lambda **_: prober)
    return entrypoint, bootstrap, ollama_pool


@pytest.mark.usefixtures("isolated_default_runtime")
@pytest.mark.parametrize("command", ["serve", "mcp"])
def test_long_lived_entrypoint_admits_configured_static_remote_worker(monkeypatch, tmp_path, command):
    import server
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.platform.config import SonderConfig, StateConfig

    config = SonderConfig(state=StateConfig(home=str(tmp_path)), ollama=OllamaConfig(
        url=LOCAL, workers=(REMOTE,), allow_remote=True, worker_capability_ttl_seconds=60))
    probes = []

    def prober(origin):
        probes.append(origin)
        return {"version": "0.33.2",
                "models": ["qwen3.6:35b"] if origin == REMOTE else ["local-model"]}

    entrypoint, bootstrap, ollama_pool = _patch_entrypoint(monkeypatch, tmp_path, config, prober)
    observed = []

    def run_interface(**_):
        pool = bootstrap.default_app().inference_pool
        deadline = time.monotonic() + 10
        # No admin refresh, no request: the owner surface alone must admit it.
        while time.monotonic() < deadline and not _remote_record(pool)["healthy"]:
            time.sleep(0.02)
        # The local primary is still unprobed here; only the remote is proven.
        observed.append((pool.summary()["eligible_worker_count"], _remote_record(pool)))

    monkeypatch.setattr(serve, "main", run_interface)
    monkeypatch.setattr(server.mcp, "run", run_interface)
    monkeypatch.setattr(server, "require_mcp_startup_safety", lambda: None)
    try:
        args = SimpleNamespace(skip_preflight=True, native=False, json=False)
        assert getattr(entrypoint, "cmd_" + command)(args) == 0
    finally:
        bootstrap.close_default_runtime_resources(timeout=2)
        ollama_pool.reset_typed_workers()
    assert len(observed) == 1
    eligible, remote = observed[0]
    assert REMOTE in probes
    assert eligible == 1
    assert remote["state"] == "ready" and remote["healthy"] is True
    assert remote["model_preview"] == ["qwen3.6:35b"] and remote["version"] == "0.33.2"


def test_unrefreshed_probation_worker_reports_a_pending_category():
    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True,
                            capability_prober=lambda _: {"models": ["m"]})
    control = controller(Source(signed_snapshot()), pool, Clock())
    try:
        remote = _remote_record(pool)
        assert remote["state"] == "probation"
        # Silent "none" hid a never-started membership lifecycle.
        assert remote["error_category"] == "membership_pending"
    finally:
        assert control.close(timeout=2)


def test_remote_tls_probe_failure_is_logged_and_categorized(caplog):
    def prober(origin):
        if origin == REMOTE:
            raise URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))
        return {"models": ["local-model"]}

    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True, capability_prober=prober)
    control = controller(Source(signed_snapshot()), pool, Clock())
    try:
        with caplog.at_level(logging.WARNING):
            control.refresh(timeout_seconds=2)
        remote = _remote_record(pool)
        assert remote["state"] == "probation" and remote["healthy"] is False
        assert remote["error_category"] == "tls"
        warnings = [record.getMessage() for record in caplog.records
                    if record.levelno == logging.WARNING and "capability probe failed" in record.getMessage()]
        assert warnings and all(len(message) <= 300 for message in warnings)
        assert any("TLS" in message for message in warnings)
    finally:
        assert control.close(timeout=2)


def test_model_miss_refreshes_fresh_static_capabilities_once_and_is_rate_limited():
    clock = Monotonic()
    inventory = {"models": ["base"]}
    probes = []

    def prober(origin):
        probes.append(origin)
        return dict(inventory)

    pool = OllamaWorkerPool(LOCAL, clock=clock, capability_ttl_seconds=300,
                            capability_prober=prober)
    pool.refresh_capabilities()
    assert probes == [LOCAL]
    inventory["models"] = ["base", "newly-pulled"]
    # Capabilities are still fresh, but they predate the pull.
    assert pool.request(lambda origin: origin, model="newly-pulled") == LOCAL
    assert probes == [LOCAL, LOCAL]
    # A second miss inside the rate-limit window must not probe again.
    with pytest.raises(WorkerCapabilityUnavailable):
        pool.request(lambda _: pytest.fail("unsupported model admitted"), model="absent")
    assert probes == [LOCAL, LOCAL]
    clock.now += 60
    with pytest.raises(WorkerCapabilityUnavailable):
        pool.request(lambda _: pytest.fail("unsupported model admitted"), model="absent")
    assert probes == [LOCAL, LOCAL, LOCAL]
    # A supported model never triggers a refresh.
    assert pool.request(lambda origin: origin, model="base") == LOCAL
    assert probes == [LOCAL, LOCAL, LOCAL]


def test_model_miss_refresh_makes_new_model_on_active_remote_member_routable():
    clock = Clock()
    inventory = {REMOTE: ["qwen3.5:4b"], LOCAL: ["local-model"]}
    probes = []

    def prober(origin):
        probes.append(origin)
        return {"models": list(inventory[origin])}

    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True, capability_prober=prober)
    control = controller(Source(signed_snapshot()), pool, clock)
    try:
        control.refresh(timeout_seconds=2)
        pool.refresh_capabilities()
        assert pool.summary()["eligible_worker_count"] == 2
        inventory[REMOTE] = ["qwen3.5:4b", "qwen3.6:35b"]
        before = len(probes)
        assert pool.request(lambda origin: origin, model="qwen3.6:35b") == REMOTE
        assert REMOTE in probes[before:]
        # Bounded: one batch, never more probes than configured workers.
        assert len(probes) - before <= 2
    finally:
        assert control.close(timeout=2)


def test_model_miss_refresh_never_probes_expired_or_unconsented_members():
    clock = Clock()
    probes = []
    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True,
                            capability_prober=lambda origin: probes.append(origin) or {"models": ["m"]})
    control = controller(Source(signed_snapshot()), pool, clock)
    try:
        control.refresh(timeout_seconds=2)
        clock.now += timedelta(seconds=61)
        before = list(probes)
        with pytest.raises(WorkerCapabilityUnavailable):
            pool.request(lambda _: pytest.fail("admitted"), model="absent")
        assert REMOTE not in probes[len(before):]
    finally:
        assert control.close(timeout=2)


@pytest.mark.parametrize("mode, remote, expected", [
    ("static", True, True), ("static", False, False), ("external", True, False),
])
def test_start_inference_membership_only_starts_static_remote_rosters(mode, remote, expected):
    from sonder_runtime.bootstrap.app import start_inference_membership

    starts = []
    application = SimpleNamespace(
        config=SimpleNamespace(membership=SimpleNamespace(mode=mode)),
        inference_pool=SimpleNamespace(has_configured_remote_workers=remote),
        inference_membership=SimpleNamespace(start=lambda **kwargs: starts.append(kwargs)),
    )
    assert start_inference_membership(application) is expected
    assert starts == ([{"refresh_now": True}] if expected else [])
    assert start_inference_membership(SimpleNamespace()) is False


def test_controller_start_rejects_non_boolean_refresh_now():
    pool = OllamaWorkerPool(LOCAL, (REMOTE,), allow_remote=True,
                            capability_prober=lambda _: {"models": ["m"]})
    control = controller(Source(signed_snapshot()), pool, Clock())
    try:
        with pytest.raises(ValueError):
            control.start(refresh_now=1)
        assert control._thread is None
    finally:
        assert control.close(timeout=2)


@pytest.mark.parametrize("variable", ["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"])
def test_config_adopts_the_process_ca_bundle_for_remote_ollama(tmp_path, variable):
    from sonder_runtime.platform.config import load_config

    bundle = tmp_path / "sonder-ca-bundle.pem"
    bundle.write_text("placeholder", encoding="ascii")
    assert load_config(env={variable: str(bundle)}).ollama.ca_bundle == str(bundle)
    # An Ollama-specific bundle still wins over the process-wide convention.
    specific = tmp_path / "ollama.pem"
    specific.write_text("placeholder", encoding="ascii")
    assert load_config(env={variable: str(bundle), "SONDER_OLLAMA_CA_BUNDLE": str(specific)}
                       ).ollama.ca_bundle == str(specific)


def test_config_ignores_an_unusable_process_ca_bundle(tmp_path):
    from sonder_runtime.platform.config import load_config

    assert load_config(env={"SSL_CERT_FILE": str(tmp_path / "missing.pem"),
                            "REQUESTS_CA_BUNDLE": "relative.pem"}).ollama.ca_bundle == ""
    assert load_config(env={}).ollama.ca_bundle == ""


def test_remote_https_verifies_with_the_configured_bundle_only(monkeypatch, tmp_path):
    import urllib.request
    from sonder_runtime.adapters.inference import ollama_endpoint

    bundle = tmp_path / "sonder-ca-bundle.pem"
    bundle.write_text("placeholder", encoding="ascii")
    monkeypatch.delenv("SONDER_OLLAMA_CA_BUNDLE", raising=False)
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    contexts, opened = [], []
    sentinel = object()
    monkeypatch.setattr(ollama_endpoint.ssl, "create_default_context",
                        lambda **kwargs: contexts.append(kwargs) or sentinel)

    class Opener:
        def open(self, request, timeout):
            opened.append((request.full_url, timeout))
            return "response"

    monkeypatch.setattr(ollama_endpoint.urllib.request, "build_opener", lambda *_handlers: Opener())
    try:
        assert ollama_endpoint.tls_trust_source() == "system-trust-store"
        ollama_endpoint.configure_typed_ca_bundle(str(bundle))
        request = urllib.request.Request(REMOTE + "/api/version")
        assert ollama_endpoint.open_url(request, timeout=1, allow_remote=True) == "response"
        # Only the configured bundle anchors trust, so a stale same-subject
        # entry in the merged OS store cannot shadow it. Verification stays on:
        # create_default_context(cafile=...) requires certificates and hostname
        # checks, and nothing here builds an unverified context.
        assert contexts == [{"cafile": str(bundle)}]
        assert opened == [(REMOTE + "/api/version", 1)]
        assert ollama_endpoint.tls_trust_source() == "configured-ca-bundle"
        monkeypatch.setenv("SONDER_OLLAMA_CA_BUNDLE", str(tmp_path / "missing.pem"))
        ollama_endpoint.configure_typed_ca_bundle(None)
        assert ollama_endpoint.tls_trust_source() == "invalid-ca-bundle"
    finally:
        ollama_endpoint.configure_typed_ca_bundle(None)


def test_real_request_waits_for_its_own_prewarm_instead_of_failing_admission(monkeypatch):
    import contextlib
    import io
    import json
    import threading

    import server
    from sonder_runtime.adapters.inference import prewarm_gate

    other = "http://127.0.0.2:11434"
    pool = OllamaWorkerPool(LOCAL, (other,), admission_timeout_seconds=0.2,
                            capability_prober=lambda origin: {
                                "models": ["qwen3.6:35b"] if origin == LOCAL else []})
    pool.refresh_capabilities()
    loading, release = threading.Event(), threading.Event()
    bodies = []

    @contextlib.contextmanager
    def open_url(request, *, timeout, allow_remote=None):
        body = json.loads(request.data)
        bodies.append(body)
        if "prompt" not in body and "messages" not in body:
            loading.set()
            assert release.wait(5)
        yield io.BytesIO(b'{"done": true}')

    monkeypatch.setattr(pool, "open_url", open_url)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "dispatch_provider", lambda _p, _path, _payload, send: send())
    monkeypatch.setattr(server, "_record_residency_dispatch", lambda *_a, **_k: None)
    assert prewarm_gate.begin("qwen3.6:35b")
    prewarm = threading.Thread(target=lambda: (
        prewarm_gate.run_as_prewarm(lambda: server._post(
            "/api/generate", {"model": "qwen3.6:35b", "keep_alive": "5m"}, timeout=10)),
        prewarm_gate.finish("qwen3.6:35b")))
    prewarm.start()
    try:
        assert loading.wait(5)
        threading.Timer(0.5, release.set).start()
        # Admission timeout is 0.2s but the prewarm holds the slot for 0.5s.
        assert server._post("/api/generate", {"model": "qwen3.6:35b", "prompt": "hi"},
                            timeout=10) == {"done": True}
    finally:
        release.set()
        prewarm.join(5)
    assert [body.get("prompt") for body in bodies] == [None, "hi"]


def test_prewarm_gate_wait_is_bounded_and_never_self_blocking():
    from sonder_runtime.adapters.inference import prewarm_gate

    assert prewarm_gate.await_prewarm("m", 0) is True
    assert prewarm_gate.begin("m") and not prewarm_gate.begin("m")
    try:
        started = time.monotonic()
        assert prewarm_gate.await_prewarm("m", 0.05) is False
        assert time.monotonic() - started < 2
        assert prewarm_gate.run_as_prewarm(lambda: prewarm_gate.await_prewarm("m", 5)) is True
        assert prewarm_gate.await_prewarm(None, 5) is True
    finally:
        prewarm_gate.finish("m")
    assert prewarm_gate.await_prewarm("m", 5) is True
