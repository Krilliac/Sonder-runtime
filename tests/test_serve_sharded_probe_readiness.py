"""Startup readiness logging and narrow exception catch for the RPC sharded backend probe."""
from __future__ import annotations

import http.client
import logging
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import ClassVar

import pytest

from sonder_runtime.adapters.inference import rpc_cluster
from sonder_runtime.adapters.inference.rpc_cluster import (
    RpcClusterConfigError,
    ShardedServerHealth,
)
from sonder_runtime.interfaces.http import serve

_RPC_BASE = "http://127.0.0.1:11438"
_RPC_PEERS = "10.77.0.2:50052"
_ENV = {"SONDER_OPENAI_BASE_URL": _RPC_BASE, "SONDER_RPC_PEERS": _RPC_PEERS}


def test_readiness_result_is_logged_with_model_ids(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    health = ShardedServerHealth(
        base_url=_RPC_BASE,
        healthy=True,
        status="ok",
        model_ids=("sonder-rpc",),
    )
    monkeypatch.setattr(rpc_cluster, "probe_configured_sharded_server", lambda _env, get_fn=None: health)
    with caplog.at_level(logging.INFO, logger=rpc_cluster._logger.name):
        result = rpc_cluster.run_sharded_backend_probe(_ENV)
    assert result is health
    messages = [r.getMessage() for r in caplog.records]
    assert any("healthy=True" in m and "['sonder-rpc']" in m for m in messages)
    assert not any("probe failed" in m for m in messages)


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionRefusedError("nope"),
        RpcClusterConfigError("not loopback"),
        http.client.IncompleteRead(b""),
    ],
)
def test_operational_failures_become_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, exc: Exception,
) -> None:
    def boom(_env, get_fn=None):
        raise exc

    monkeypatch.setattr(rpc_cluster, "probe_configured_sharded_server", boom)
    with caplog.at_level(logging.WARNING, logger=rpc_cluster._logger.name):
        assert rpc_cluster.run_sharded_backend_probe(_ENV) is None
    assert any(type(exc).__name__ in r.getMessage() for r in caplog.records)


def test_programming_errors_propagate_from_direct_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_env, get_fn=None):
        raise AttributeError("boom")

    monkeypatch.setattr(rpc_cluster, "probe_configured_sharded_server", broken)
    with pytest.raises(AttributeError, match="boom"):
        rpc_cluster.run_sharded_backend_probe(_ENV)


def test_background_probe_wrapper_logs_unexpected_error_without_crashing_thread(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(_env, get_fn=None):
        raise AttributeError("boom")

    monkeypatch.setattr(rpc_cluster, "probe_configured_sharded_server", broken)
    threads: list[threading.Thread] = []

    def tracking_thread(**kwargs):
        t = threading.Thread(**kwargs)
        threads.append(t)
        return t

    with caplog.at_level(logging.ERROR, logger=rpc_cluster._logger.name):
        assert rpc_cluster.start_sharded_backend_probe(thread_factory=tracking_thread, env=_ENV) is True
        assert len(threads) == 1
        threads[0].join(timeout=5)
        assert not threads[0].is_alive()
    assert any("unexpected error" in r.getMessage() for r in caplog.records)


def _deep_get(_url: str, _timeout: float) -> tuple[int, bytes]:
    return 200, b"[" * 200000


def test_deeply_nested_reply_becomes_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=rpc_cluster._logger.name):
        assert rpc_cluster.run_sharded_backend_probe(_ENV, get_fn=_deep_get) is None
    assert any("probe failed (RecursionError)" in r.getMessage() for r in caplog.records)


def test_unhealthy_status_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    health = ShardedServerHealth(base_url=_RPC_BASE, healthy=False, status="down", error="x")
    monkeypatch.setattr(rpc_cluster, "probe_configured_sharded_server", lambda _env, get_fn=None: health)
    with caplog.at_level(logging.INFO, logger=rpc_cluster._logger.name):
        result = rpc_cluster.run_sharded_backend_probe(_ENV)
    assert result is health
    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == rpc_cluster._logger.name]
    assert (logging.WARNING, "RPC sharded backend: healthy=False status=down models=[] error=x") in records
    assert not any("probe failed" in m for _lvl, m in records)


class _FakeServer:
    instances: ClassVar[list[_FakeServer]] = []

    def __init__(self, _address, _handler):
        self.served = False
        self.closed = False
        type(self).instances.append(self)

    def serve_forever(self):
        self.served = True

    def server_close(self):
        self.closed = True

    def shutdown(self):
        return None


def _run_main(tmp_path, monkeypatch, probe, *, base=_RPC_BASE, peers=_RPC_PEERS):
    from sonder_runtime.bootstrap import app as bootstrap_app
    from tests.test_memory_replication_service import _config

    bootstrap_app.reset_for_tests()
    _FakeServer.instances = []
    lifecycle = SimpleNamespace(
        startup=lambda **_kwargs: None,
        begin_ollama_probe=lambda: None,
        stop_probe=lambda: None,
        coordinator=SimpleNamespace(add_flush_hook=lambda _hook: None, draining=False),
        drain=lambda _reason: True,
    )
    for name in (
        "_SESSION_FACADE", "_CONTROL_PLANE_SERVICE",
        "_MEMORY_REPLICATION_RECEIVER", "_MEMORY_REPLICATION_SERVICE",
        "_ARTIFACT_TRANSFER_BINDING", "_APP_CONTROL_BINDING",
    ):
        monkeypatch.setattr(serve, name, None)
    monkeypatch.setattr(serve.sonder_lifecycle, "get", lambda: lifecycle)
    monkeypatch.setattr(
        serve._legacy_runtime(), "runtime_source_update_status", lambda refresh=False: "ok",
    )
    if base is None:
        monkeypatch.delenv("SONDER_OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("SONDER_OPENAI_BASE_URL", base)
    if peers is None:
        monkeypatch.delenv("SONDER_RPC_PEERS", raising=False)
    else:
        monkeypatch.setenv("SONDER_RPC_PEERS", peers)

    spawned: list[threading.Thread] = []
    real_thread_factory = serve.owned_runtime_thread

    def capturing_thread(**kwargs):
        t = real_thread_factory(**kwargs)
        spawned.append(t)
        return t

    monkeypatch.setattr(serve, "owned_runtime_thread", capturing_thread)
    calls: list[str] = []

    def recording_probe(url, *args, **kwargs):
        calls.append(url)
        return probe(url)

    monkeypatch.setattr(rpc_cluster, "probe_sharded_server", recording_probe)
    try:
        config = _config(tmp_path, receiver_enabled=False)
        config = replace(config, server=replace(config.server, prewarm_on_start=False))
        serve.main(config, _server_factory=_FakeServer)
    finally:
        for t in spawned:
            if t.is_alive():
                t.join(timeout=5)
        bootstrap_app.reset_for_tests()
    return calls


def test_main_starts_sharded_probe_and_logs_status(tmp_path, monkeypatch, caplog):
    health = ShardedServerHealth(base_url=_RPC_BASE, healthy=True, status="ok", model_ids=("sonder-rpc",))
    with caplog.at_level(logging.INFO, logger=rpc_cluster._logger.name):
        calls = _run_main(tmp_path, monkeypatch, lambda _url: health)
    assert calls == [_RPC_BASE]
    assert any("healthy=True" in r.getMessage() and "['sonder-rpc']" in r.getMessage() for r in caplog.records)
    assert _FakeServer.instances and _FakeServer.instances[0].served


def test_main_downgrades_unexpected_probe_exception_in_background_and_serves(tmp_path, monkeypatch, caplog):
    def broken(_url):
        raise AttributeError("boom")

    with caplog.at_level(logging.ERROR, logger=rpc_cluster._logger.name):
        calls = _run_main(tmp_path, monkeypatch, broken)
    assert calls == [_RPC_BASE]
    assert any("unexpected error" in r.getMessage() for r in caplog.records)
    assert _FakeServer.instances and _FakeServer.instances[0].served
