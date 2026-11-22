"""Model discovery must not depend on which pool member the scheduler picked.

Observed 2026-11-16: with the workstation saturated every ``/api/tags`` read
went to Node1, so ``kimi-k3:cloud`` (held only by the workstation) was refused
with ``400 unknown model`` and dropped from ``/v1/models``.
"""
from __future__ import annotations

import io
import json
from collections import Counter
from urllib.error import URLError

import pytest

from sonder_runtime.adapters.inference.external_membership import ExternalMembershipSource
from sonder_runtime.adapters.inference.ollama_pool import OllamaWorkerPool, WorkerPoolUnavailable
from sonder_runtime.interfaces.http import serve

from tests.test_external_inference_membership import (  # noqa: F401 - autouse fixture
    NOW as EXTERNAL_NOW,
    ORIGIN as EXTERNAL_ORIGIN,
    configuration,
    credentials,
    private_authority_material,
)

LOCAL = "http://127.0.0.1:11434"
NODE1 = "https://node1.example:8443"
CATALOGS = {
    LOCAL: {"models": [{"name": "kimi-k3:cloud", "capabilities": ["completion"]},
                       {"name": "shared:latest", "capabilities": ["completion"]}]},
    NODE1: {"models": [{"name": "qwen3.6:35b", "capabilities": ["completion"]},
                       {"name": "shared:latest", "capabilities": ["embedding"]}]},
}


@pytest.fixture
def pooled(monkeypatch):
    import server

    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True)
    assert pool.enabled
    calls = {"get": 0}

    def busy_get(path, *args, **kwargs):
        # The scheduler picked Node1 because the workstation is saturated.
        calls["get"] += 1
        return CATALOGS[NODE1]

    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "_cloud_allowed_policy", lambda env: True)
    monkeypatch.setattr(server, "_get", busy_get)
    monkeypatch.setattr(server, "_pool_member_tags", lambda origin, timeout=15: CATALOGS[origin])
    return server, pool, calls


def test_model_held_by_the_busy_member_is_still_known(pooled):
    server, _pool, _calls = pooled
    assert "kimi-k3:cloud" in server.discovered_models()
    assert "qwen3.6:35b" in server.discovered_models()
    assert serve._chat_model_selection_error("kimi-k3:cloud") is None
    ids = {row["id"] for row in serve._openai_model_rows()}
    assert "kimi-k3:cloud" in ids


def test_pool_disabled_uses_a_single_get(monkeypatch):
    import server

    pool = OllamaWorkerPool(LOCAL, (), allow_remote=True)
    assert not pool.enabled
    seen = []
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_get", lambda path, *a, **k: seen.append(path) or CATALOGS[LOCAL])
    monkeypatch.setattr(server, "_pool_member_tags",
                        lambda *a, **k: pytest.fail("pool must not be read when disabled"))
    assert "kimi-k3:cloud" in server.discovered_models()
    assert seen == ["/api/tags"]


def test_one_unreachable_member_is_skipped_all_unreachable_is_503(pooled, monkeypatch):
    server, _pool, _calls = pooled

    def one_down(origin, timeout=15):
        if origin == NODE1:
            raise URLError("node1 offline")
        return CATALOGS[origin]

    monkeypatch.setattr(server, "_pool_member_tags", one_down)
    assert "kimi-k3:cloud" in server.discovered_models()
    assert "qwen3.6:35b" not in server.discovered_models()

    def all_down(origin, timeout=15):
        raise URLError("down")

    monkeypatch.setattr(server, "_pool_member_tags", all_down)
    # Every member down: the pooled read the primary falls back to fails too.
    monkeypatch.setattr(server, "_get", lambda path, *a, **k: all_down(path))
    with pytest.raises(URLError):
        server.discovered_models()
    error = serve._chat_model_selection_error("ghost-model:1b")
    assert error is not None and error[0] == 503


def test_primary_wins_a_shared_name(pooled):
    server, _pool, _calls = pooled
    name, record = server.resolve_discovered_model_record("shared:latest")
    assert name == "shared:latest"
    assert record["capabilities"] == ["completion"]


def _assert_listed_models_pass_the_chat_gate(server):
    """``/v1/models`` offers every row in a picker; each must be selectable."""
    tiers = {"sonder", *server.available_tiers()}
    ids = [row["id"] for row in serve._openai_model_rows()]
    for identifier in ids:
        if identifier not in tiers:
            assert serve._chat_model_selection_error(identifier) is None, identifier
    return set(ids)


def test_listed_models_are_exactly_what_the_chat_gate_accepts(pooled):
    server, _pool, _calls = pooled
    # Node1 holds qwen3.6:35b; with pool-wide target resolution active, the HTTP
    # chat gate accepts worker-only models while refusing unknown names, and
    # every row offered by /v1/models passes the chat gate.
    assert serve._chat_model_selection_error("qwen3.6:35b") is None
    assert serve._chat_model_selection_error("not-on-any-worker:9b") == (
        400, "unknown model 'not-on-any-worker:9b'"
    )
    ids = _assert_listed_models_pass_the_chat_gate(server)
    assert "kimi-k3:cloud" in ids


@pytest.mark.parametrize("failure", [
    URLError("primary offline"),
    TimeoutError("primary hung"),
    WorkerPoolUnavailable("primary not admitted"),
])
def test_primary_down_falls_back_to_the_pooled_catalog(pooled, monkeypatch, failure):
    server, _pool, calls = pooled

    def primary_down(origin, timeout=15):
        if origin == LOCAL:
            raise failure
        return CATALOGS[origin]

    monkeypatch.setattr(server, "_pool_member_tags", primary_down)
    # The pooled read fails over to Node1, as it did before the union change.
    assert server.resolve_discovered_model_record("qwen3.6:35b")[0] == "qwen3.6:35b"
    assert serve._chat_model_selection_error("qwen3.6:35b") is None
    assert server._serve_target("qwen3.6:35b", None)[3] is not None
    ids = _assert_listed_models_pass_the_chat_gate(server)
    assert "qwen3.6:35b" in ids
    assert calls["get"] > 0


def test_external_membership_without_local_fallback_reads_the_pooled_catalog(
    tmp_path, monkeypatch,
):
    import server

    source = ExternalMembershipSource(configuration(tmp_path), credentials(tmp_path),
                                      clock=lambda: EXTERNAL_NOW)
    assert source._config.local_fallback is False
    try:
        pool = OllamaWorkerPool(LOCAL, (EXTERNAL_ORIGIN,), allow_remote=True)
        pool.configure_external_source(source)
        assert pool.enabled
        opened = []

        def open_url(request, *, timeout, allow_remote=None):
            opened.append(request.full_url)
            raise AssertionError("an inadmissible primary must not be read")

        monkeypatch.setattr(pool, "open_url", open_url)
        worker = {"models": [{"name": "qwen3.6:35b", "capabilities": ["completion"]}]}
        monkeypatch.setattr(server, "OLLAMA_POOL", pool)
        monkeypatch.setattr(server, "BASE", LOCAL)
        monkeypatch.setattr(server, "_cloud_allowed_policy", lambda env: True)
        monkeypatch.setattr(server, "_get", lambda path, *a, **k: worker)
        assert server.resolve_discovered_model_record("qwen3.6:35b")[0] == "qwen3.6:35b"
        assert serve._chat_model_selection_error("qwen3.6:35b") is None
        assert "qwen3.6:35b" in _assert_listed_models_pass_the_chat_gate(server)
        assert opened == []
    finally:
        assert source.close(timeout=2)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_saturated_primary_is_read_directly_without_a_pool_slot(monkeypatch):
    """The real pool and ``_get``: only the transport is faked, per origin."""
    import server

    pool = OllamaWorkerPool(LOCAL, (NODE1,), allow_remote=True)
    reads = Counter()

    def open_url(request, *, timeout, allow_remote=None):
        origin = next(o for o in CATALOGS if request.full_url.startswith(o))
        reads[origin] += 1
        return _Response(json.dumps(CATALOGS[origin]).encode())

    monkeypatch.setattr(pool, "open_url", open_url)
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "BASE", LOCAL)
    monkeypatch.setattr(server, "_cloud_allowed_policy", lambda env: True)
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **k: None)
    primary = pool._states[0]
    assert primary.endpoint.origin == LOCAL
    primary.inflight = pool._capacity(primary)  # the incident: no pool slot is free

    assert server._get("/api/tags") == CATALOGS[NODE1]  # a pooled read goes to Node1
    reads.clear()
    assert serve._chat_model_selection_error("kimi-k3:cloud") is None
    assert reads == {LOCAL: reads[LOCAL]} and reads[LOCAL] >= 1
    reads.clear()
    ids = _assert_listed_models_pass_the_chat_gate(server)
    assert "kimi-k3:cloud" in ids and "qwen3.6:35b" not in ids
    assert set(reads) == {LOCAL}
    assert primary.inflight == pool._capacity(primary)
