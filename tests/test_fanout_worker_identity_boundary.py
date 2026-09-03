"""Boundary tests for WP1 fanout_worker_identity migration."""
import server
from sonder_runtime.domain import fanout_worker_identity


def test_root_constant_is_identity_preserving_alias():
    assert server._FANOUT_WORKER_INSTANCE is fanout_worker_identity.FANOUT_WORKER_INSTANCE


def test_delegate_produces_same_output_as_packaged_function():
    import os, threading
    expected = fanout_worker_identity.fanout_worker_id(
        fanout_worker_identity.FANOUT_WORKER_INSTANCE,
        os.getpid(),
        threading.get_ident(),
    )
    assert server._fanout_worker_id() == expected


def test_worker_id_format():
    wid = server._fanout_worker_id()
    assert wid.startswith("fanout-")
    parts = wid.split("-", 1)
    assert len(parts) == 2
    assert parts[1]


def test_worker_id_is_stable_within_process():
    a = server._fanout_worker_id()
    b = server._fanout_worker_id()
    assert a == b


def test_instance_token_is_hex():
    token = fanout_worker_identity.FANOUT_WORKER_INSTANCE
    assert isinstance(token, str)
    assert len(token) == 32
    int(token, 16)


def test_monkeypatch_surfaces_survive(monkeypatch):
    """The delegate reads server-level names, so monkeypatching works."""
    monkeypatch.setattr(server, "_FANOUT_WORKER_INSTANCE", "test-instance")
    monkeypatch.setattr(server.os, "getpid", lambda: 42)
    monkeypatch.setattr(server.threading, "get_ident", lambda: 7)
    assert server._fanout_worker_id() == "fanout-test-instance-42-7"
