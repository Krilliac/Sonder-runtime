"""A model called by name keeps one context window across /api/show failures.

Ollama reloads the whole model whenever ``num_ctx`` changes between requests,
so a by-name call that is sized 8192, then 16384, then 32768 depending on
whether the metadata probe happened to succeed costs a full reload per turn.
"""
import urllib.error
from pathlib import Path

import pytest

import server
from sonder_runtime.platform import context_policy

MODEL = "astra-cyber-27b"
WORKTREE = Path(__file__).resolve().parents[1]

SHOW_WITH_DECLARED = {
    "details": {"parameter_size": "26.9B"},
    "model_info": {"x.context_length": 262144},
    "parameters": "num_ctx                        8192",
}
SHOW_WITHOUT_DECLARED = {
    "details": {"parameter_size": "26.9B"},
    "model_info": {"x.context_length": 262144},
}


def _not_found():
    return urllib.error.HTTPError(
        "http://worker/api/show", 404, "model not found", {}, None,
    )


def _clear_caches():
    server._MODEL_CONTEXT_CACHE.clear()
    server._MODEL_DECLARED_CONTEXT_CACHE.clear()
    server._MODEL_GEOMETRY_CACHE.clear()
    server._MODEL_LAST_SUCCESS_METADATA.clear()
    server._MODEL_UNPROBED_LOGGED.clear()


@pytest.fixture(autouse=True)
def _isolated_context_environment(monkeypatch):
    for name in ("SONDER_CONTEXT_SIZE", "SONDER_SESSION_NUM_CTX", "SONDER_KV_CACHE_TYPE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_KV_CACHE_TYPE", "q8_0")
    monkeypatch.setattr(server, "_residency_feedback", lambda: None)
    _clear_caches()
    yield
    _clear_caches()


def _expire_cache():
    for key, (stamp, ctx, params) in list(server._MODEL_CONTEXT_CACHE.items()):
        server._MODEL_CONTEXT_CACHE[key] = (
            stamp - server._MODEL_CONTEXT_CACHE_TTL - 1, ctx, params,
        )


def _chat_context(monkeypatch, show_results):
    """Run one by-name chat per entry of show_results; return each num_ctx.

    An entry is a dict (a successful /api/show) or None (a 404).  The metadata
    cache is expired between turns so every turn re-probes.
    """
    sent = []
    results = iter(show_results)

    def fake_post(path, payload, **_kwargs):
        if path == "/api/show":
            result = next(results)
            if result is None:
                raise _not_found()
            return result
        sent.append(payload["options"]["num_ctx"])
        return {"message": {"content": "ok"}}

    monkeypatch.setattr(server, "_post", fake_post)
    for _ in show_results:
        server._make_generate(MODEL, "", 0.2, 32, 0, text_only=True)("hello")
        _expire_cache()
    return sent


def test_tests_import_the_worktree_code():
    """Guard: the venv may resolve sonder_runtime/server from production."""
    server_file = Path(server.__file__).resolve()
    policy_file = Path(context_policy.__file__).resolve()
    assert WORKTREE in server_file.parents, server_file
    assert WORKTREE in policy_file.parents, policy_file


def test_by_name_model_keeps_declared_context_across_probe_failures(monkeypatch):
    sent = _chat_context(monkeypatch, [SHOW_WITH_DECLARED, None, None])
    assert len(sent) == 3
    assert sent == [8192, 8192, 8192], f"num_ctx changed between turns: {sent}"


def test_failed_probe_never_yields_a_larger_context_than_a_successful_one(monkeypatch):
    succeeded = _chat_context(monkeypatch, [SHOW_WITHOUT_DECLARED])
    _clear_caches()
    failed = _chat_context(monkeypatch, [None])
    assert len(succeeded) == len(failed) == 1
    assert failed[0] <= succeeded[0], (
        f"failed probe sent num_ctx={failed[0]}, successful probe {succeeded[0]}"
    )


def test_context_probe_carries_the_model_as_the_routing_hint(monkeypatch):
    class RecordingPool:
        enabled = True

        def __init__(self):
            self.models = []

        def request(self, send, model=None, idempotent=False, payload=None):
            self.models.append(model)
            return SHOW_WITH_DECLARED

    pool = RecordingPool()
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda: None)

    server._model_context_metadata(MODEL)

    assert pool.models == [MODEL], f"routing hints sent with /api/show: {pool.models}"


def test_small_model_without_declared_context_keeps_one_window_across_a_failed_probe(monkeypatch):
    small = {
        "details": {"parameter_size": "7.6B"},
        "model_info": {"x.context_length": 32768},
    }
    sent = _chat_context(monkeypatch, [small, None, None])
    assert sent == [32768, 32768, 32768], sent


def test_never_probed_model_gets_the_conservative_window(monkeypatch):
    assert _chat_context(monkeypatch, [None]) == [context_policy.METADATA_UNAVAILABLE_CONTEXT]


def test_declared_value_is_a_ceiling_never_a_raise(monkeypatch):
    show = {
        "details": {"parameter_size": "4.2B"},
        "model_info": {"x.context_length": 262144},
        "parameters": "num_ctx                        131072",
    }
    assert _chat_context(monkeypatch, [show, None]) == [32768, 32768]


def test_declared_value_is_clamped_to_the_advertised_length_and_survives_failure(monkeypatch):
    show = {
        "details": {"parameter_size": "1B"},
        "model_info": {"x.context_length": 2048},
        "parameters": "num_ctx 8192",
    }
    assert _chat_context(monkeypatch, [show, None]) == [2048, 2048]
    assert server._MODEL_DECLARED_CONTEXT_CACHE[MODEL] == 2048


def test_operator_pin_beats_the_declared_value(monkeypatch):
    monkeypatch.setenv("SONDER_CONTEXT_SIZE", "16384")
    sent = _chat_context(monkeypatch, [SHOW_WITH_DECLARED])
    assert sent == [16384]


def test_cloud_model_name_skips_the_declared_path(monkeypatch):
    monkeypatch.setattr(server, "_MODEL_DECLARED_CONTEXT_CACHE", {"qwen3-coder:cloud": 4096})
    assert server._is_cloud_model_name("qwen3-coder:cloud")
    assert server._auto_model_context("qwen3-coder:cloud") != 4096
