"""Startup readiness logging for the RPC sharded backend probe."""
from __future__ import annotations

import http.client
import logging
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference import rpc_cluster
from sonder_runtime.adapters.inference.rpc_cluster import (
    RpcClusterConfigError,
    ShardedServerHealth,
)
from sonder_runtime.interfaces.http import serve


def test_readiness_result_is_logged_with_model_ids(caplog: pytest.LogCaptureFixture) -> None:
    health = ShardedServerHealth(
        base_url="http://127.0.0.1:11438",
        healthy=True,
        status="ok",
        model_ids=("sonder-rpc",),
    )
    with caplog.at_level(logging.INFO, logger=serve._serve_logger.name):
        result = serve._log_sharded_backend_readiness(
            "http://127.0.0.1:11438", probe=lambda _url: health
        )
    assert result is health
    messages = [r.getMessage() for r in caplog.records]
    assert any("healthy=True" in m and "['sonder-rpc']" in m for m in messages)
    assert not any("probe failed" in m for m in messages)


def test_transport_and_config_failures_become_warning(caplog: pytest.LogCaptureFixture) -> None:
    def refuse(_url: str) -> ShardedServerHealth:
        raise ConnectionRefusedError("nope")

    def bad_config(_url: str) -> ShardedServerHealth:
        raise RpcClusterConfigError("not loopback")

    with caplog.at_level(logging.WARNING, logger=serve._serve_logger.name):
        assert serve._log_sharded_backend_readiness("http://127.0.0.1:1", probe=refuse) is None
        assert serve._log_sharded_backend_readiness("http://10.0.0.1:1", probe=bad_config) is None
    messages = [r.getMessage() for r in caplog.records]
    assert any("ConnectionRefusedError" in m for m in messages)
    assert any("RpcClusterConfigError" in m for m in messages)


def test_programming_errors_propagate() -> None:
    def broken(_url: str) -> ShardedServerHealth:
        raise AttributeError("boom")

    with pytest.raises(AttributeError, match="boom"):
        serve._log_sharded_backend_readiness("http://127.0.0.1:1", probe=broken)


def _deep_get(_url: str, _timeout: float) -> tuple[int, bytes]:
    return 200, b"[" * 200000


def test_deeply_nested_reply_becomes_warning(caplog: pytest.LogCaptureFixture) -> None:
    def probe(url: str) -> ShardedServerHealth:
        return rpc_cluster.probe_sharded_server(url, get_fn=_deep_get)

    with caplog.at_level(logging.INFO, logger=serve._serve_logger.name):
        assert serve._log_sharded_backend_readiness("http://127.0.0.1:11438", probe=probe) is None
    assert any("probe failed (RecursionError)" in r.getMessage() for r in caplog.records)


def test_unhealthy_status_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    health = ShardedServerHealth(base_url="http://127.0.0.1:11438", healthy=False, error="x")
    with caplog.at_level(logging.INFO, logger=serve._serve_logger.name):
        result = serve._log_sharded_backend_readiness(
            "http://127.0.0.1:11438", probe=lambda _url: health
        )
    assert result is health
    records = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == serve._serve_logger.name]
    assert (logging.WARNING, "RPC sharded backend status: healthy=False models=[]") in records
    assert not any("probe failed" in m for _lvl, m in records)


_real_probe = rpc_cluster.probe_sharded_server
_RPC_BASE = "http://127.0.0.1:11438"
_RPC_PEERS = "10.77.0.2:50052"


class _FakeServer:
    instances: list["_FakeServer"] = []

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
    calls = []

    def recording_probe(url, *args, **kwargs):
        calls.append(url)
        return probe(url)

    monkeypatch.setattr(rpc_cluster, "probe_sharded_server", recording_probe)
    try:
        config = _config(tmp_path, receiver_enabled=False)
        config = replace(config, server=replace(config.server, prewarm_on_start=False))
        serve.main(config, _server_factory=_FakeServer)
    finally:
        bootstrap_app.reset_for_tests()
    return calls


def _messages(caplog):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.name == serve._serve_logger.name]


def test_main_logs_sharded_status_with_model_ids(tmp_path, monkeypatch, caplog):
    health = ShardedServerHealth(base_url=_RPC_BASE, healthy=True, status="ok", model_ids=("sonder-rpc",))
    with caplog.at_level(logging.INFO, logger=serve._serve_logger.name):
        calls = _run_main(tmp_path, monkeypatch, lambda _url: health)
    assert calls == [_RPC_BASE]
    msgs = _messages(caplog)
    assert (logging.INFO, "RPC sharded backend status: healthy=True models=['sonder-rpc']") in msgs
    assert not any("probe failed" in m for _lvl, m in msgs)
    assert _FakeServer.instances and _FakeServer.instances[0].served


def test_main_downgrades_transport_failure_and_still_serves(tmp_path, monkeypatch, caplog):
    def broken(_url):
        raise http.client.IncompleteRead(b"")

    with caplog.at_level(logging.INFO, logger=serve._serve_logger.name):
        calls = _run_main(tmp_path, monkeypatch, broken)
    assert calls == [_RPC_BASE]
    msgs = _messages(caplog)
    assert (logging.WARNING, "RPC sharded backend probe failed (IncompleteRead)") in msgs
    assert _FakeServer.instances[0].served


@pytest.mark.parametrize("base,peers", [(None, _RPC_PEERS), (_RPC_BASE, None)])
def test_main_skips_probe_without_both_settings(tmp_path, monkeypatch, caplog, base, peers):
    def never(_url):
        pytest.fail("probe must not run")

    with caplog.at_level(logging.INFO, logger=serve._serve_logger.name):
        calls = _run_main(tmp_path, monkeypatch, never, base=base, peers=peers)
    assert calls == []
    assert not any("RPC sharded backend" in m for _lvl, m in _messages(caplog))
    assert _FakeServer.instances[0].served


def test_main_downgrades_deeply_nested_reply_and_still_serves(tmp_path, monkeypatch, caplog):
    def deep(url):
        return _real_probe(url, get_fn=_deep_get)

    with caplog.at_level(logging.INFO, logger=serve._serve_logger.name):
        calls = _run_main(tmp_path, monkeypatch, deep)
    assert calls == [_RPC_BASE]
    assert (logging.WARNING, "RPC sharded backend probe failed (RecursionError)") in _messages(caplog)
    assert _FakeServer.instances[0].served


def test_main_propagates_probe_programming_error_and_closes_listener(tmp_path, monkeypatch):
    def broken(_url):
        raise AttributeError("boom")

    with pytest.raises(AttributeError, match="boom"):
        _run_main(tmp_path, monkeypatch, broken)
    assert _FakeServer.instances[0].closed
    assert not _FakeServer.instances[0].served
    assert serve.BOUND_PORT is None
