"""Ollama endpoints are labelled with what they are and what they hold.

Two regressions with a loopback primary plus a remote pool worker (Node1):

* ``diagnostics`` printed the remote worker's model list on the line that named
  the loopback endpoint, because ``_get`` may be answered by any pool member.
* model errors printed "remote Ollama at http://127.0.0.1:11434", because the
  label used whole-pool locality while the address printed was the primary's.
"""
import threading
import time
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters.inference import ollama_reporting
from sonder_runtime.adapters.model_transport import ModelCallError


LOCAL = "http://127.0.0.1:11434"
REMOTE = "http://10.20.30.40:11434"


def _pool(*, enabled=True, origins=(LOCAL, REMOTE), remote=True):
    return SimpleNamespace(enabled=enabled, origins=origins, has_remote_workers=remote)


def test_pool_diagnostics_lists_each_endpoint_with_its_own_models(monkeypatch):
    catalogs = {
        LOCAL: {"models": [{"name": "local-small:3b"}]},
        REMOTE: {"models": [{"name": "remote-coder:30b"}, {"name": "remote-general:14b"}]},
    }
    monkeypatch.setattr(server, "OLLAMA_POOL", _pool())
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_k: None)
    monkeypatch.setattr(server, "_pool_member_tags", lambda origin: catalogs[origin])
    monkeypatch.setattr(server, "_get", lambda _path: pytest.fail("pool diagnostics used an unattributed _get"))

    lines = server._diagnostics_ollama_model_lines()

    assert lines == [
        "  ollama @ %s: ok (1 models: local-small:3b)" % LOCAL,
        "  ollama @ %s: ok (2 models: remote-coder:30b, remote-general:14b)" % REMOTE,
    ]


def test_pool_diagnostics_reports_an_unreachable_member_on_its_own_line(monkeypatch):
    def tags(origin):
        if origin == REMOTE:
            raise OSError("unreachable")
        return {"models": [{"name": "local-small:3b"}]}

    monkeypatch.setattr(server, "OLLAMA_POOL", _pool())
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_k: None)
    monkeypatch.setattr(server, "_pool_member_tags", tags)

    lines = server._diagnostics_ollama_model_lines()

    assert lines[0] == "  ollama @ %s: ok (1 models: local-small:3b)" % LOCAL
    assert lines[1] == "  ollama @ %s: ERROR unreachable" % REMOTE


def test_single_endpoint_diagnostics_line_is_unchanged(monkeypatch):
    monkeypatch.setattr(server, "OLLAMA_POOL", _pool(enabled=False, origins=(LOCAL,), remote=False))
    monkeypatch.setattr(server, "_get", lambda _path: {"models": [{"name": "alpha:latest"}]})
    assert server._diagnostics_ollama_model_lines() == ["  ollama: ok (1 models: alpha:latest)"]


@pytest.mark.parametrize("base", [LOCAL, "http://localhost:11434"])
def test_loopback_primary_error_is_labelled_local_even_with_remote_workers(monkeypatch, base):
    monkeypatch.setattr(server, "BASE", base)
    monkeypatch.setattr(server, "OLLAMA_POOL", _pool())
    error = ModelCallError("connection", "connection refused")

    for message in (
        server._format_model_call_error(error),
        server._format_runtime_model_call_error_policy(error, **server._ollama_error_endpoint()),
    ):
        assert message.startswith("ERROR contacting local Ollama at "), message
        assert "remote Ollama" not in message
        assert "worker pool also routes to remote workers" in message


def test_offload_error_for_loopback_primary_is_labelled_local(monkeypatch):
    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "OLLAMA_POOL", _pool())

    def fail(*_a, **_k):
        raise ModelCallError("connection", "connection refused")

    monkeypatch.setattr(server, "_offload_impl", fail)
    message = server.offload("hello", tier="fast")
    assert message.startswith("ERROR contacting local Ollama at %s" % LOCAL), message


def test_remote_primary_error_is_still_labelled_remote(monkeypatch):
    monkeypatch.setattr(server, "BASE", REMOTE)
    monkeypatch.setattr(server, "OLLAMA_POOL", _pool(origins=(REMOTE,)))
    message = server._format_model_call_error(ModelCallError("connection", "connection refused"))
    assert message.startswith("ERROR contacting remote Ollama at %s" % REMOTE), message


def test_local_only_error_display_is_unchanged(monkeypatch):
    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "OLLAMA_POOL", _pool(enabled=False, origins=(LOCAL,), remote=False))
    message = server._format_model_call_error(ModelCallError("connection", "connection refused"))
    assert message == "ERROR contacting local Ollama at %s after 1 attempt(s): connection refused" % LOCAL


def _inventory(pool, member_tags, **kwargs):
    return ollama_reporting.model_inventory_lines(
        pool, get_tags=lambda: pytest.fail("pool path used get_tags"),
        member_tags=member_tags, require_endpoint=lambda: None,
        names=lambda payload: [row["name"] for row in payload["models"]], **kwargs)


def test_pool_members_are_read_concurrently():
    origins = (LOCAL, REMOTE, "http://10.20.30.41:11434")
    together = threading.Barrier(len(origins), timeout=2)

    def tags(origin):
        together.wait()  # breaks unless every member is being read at once
        return {"models": [{"name": "m"}]}

    lines = _inventory(_pool(origins=origins), tags, deadline_seconds=3)
    assert all(line.endswith("ok (1 models: m)") for line in lines), lines


def test_slow_pool_member_times_out_within_the_overall_deadline():
    release = threading.Event()

    def tags(origin):
        if origin == REMOTE:
            release.wait(5)
        return {"models": [{"name": "local-small:3b"}]}

    started = time.monotonic()
    try:
        lines = _inventory(_pool(), tags, deadline_seconds=0.3)
    finally:
        release.set()
    assert time.monotonic() - started < 2
    assert lines[0] == "  ollama @ %s: ok (1 models: local-small:3b)" % LOCAL
    assert lines[1] == "  ollama @ %s: ERROR timed out (no answer within 0.3s)" % REMOTE
