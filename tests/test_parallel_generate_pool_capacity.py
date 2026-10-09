"""Parallel generation fan-outs never exceed the Ollama pool's model capacity.

Regression: with pool capacity 1 for the tier's model and max_workers >= 2, the
surplus candidate waited only the short interactive admission window and failed
with "timed out waiting for Ollama worker capacity". The fan-out is now clamped
to the pool capacity and the result line reports the effective worker count.
"""
from types import SimpleNamespace

import pytest

import server


class _Pool:
    enabled = True
    has_remote_workers = False

    def __init__(self, capacity):
        self.capacity = capacity
        self.asked = []

    def model_capacity(self, model):
        self.asked.append(model)
        return self.capacity


def _stub_generation(monkeypatch, pool):
    sizes = []
    real_pool = server.owned_runtime_pool

    def recording_pool(*, max_workers):
        sizes.append(max_workers)
        return real_pool(max_workers=max_workers)

    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    monkeypatch.setattr(server, "owned_runtime_pool", recording_pool)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda *_a, **_k: None)
    monkeypatch.setitem(server.TIERS, "code", "example-coder:30b")
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda _prompt: "```python\nprint(1)\n```")
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: (True, "1\n"))
    monkeypatch.setattr(server.grounding, "run_language_code", lambda *a, **k: (True, "1\n"))
    return sizes


@pytest.mark.parametrize("multi_language", [False, True])
def test_fanout_is_clamped_to_pool_capacity_and_reports_it(monkeypatch, multi_language):
    pool = _Pool(1)
    sizes = _stub_generation(monkeypatch, pool)
    if multi_language:
        out = server.parallel_generate_run_languages(
            "print one", languages="python", variants_per_language=3, max_workers=3)
    else:
        out = server.parallel_generate_run("print one", variants=3, max_workers=3)

    assert sizes == [1]
    assert pool.asked == ["example-coder:30b"]
    assert "3/3 passed" in out
    assert "workers=1, requested 3, clamped to available Ollama pool capacity 1" in out.splitlines()[0]


def test_fanout_within_capacity_is_unchanged(monkeypatch):
    sizes = _stub_generation(monkeypatch, _Pool(4))
    out = server.parallel_generate_run("print one", variants=2, max_workers=2)
    assert sizes == [2]
    assert out.splitlines()[0].endswith("(tier=code, workers=2)")


@pytest.mark.parametrize("pool", [
    _Pool(None),  # no capability evidence yet: unknown is not zero
    SimpleNamespace(enabled=False, has_remote_workers=False, model_capacity=lambda _m: pytest.fail("pool consulted")),
])
def test_fanout_is_not_clamped_without_pool_evidence(monkeypatch, pool):
    sizes = _stub_generation(monkeypatch, pool)
    out = server.parallel_generate_run("print one", variants=3, max_workers=3)
    assert sizes == [3]
    assert out.splitlines()[0].endswith("(tier=code, workers=3)")


def test_bridged_tier_is_not_clamped_by_the_ollama_pool(monkeypatch):
    pool = _Pool(1)
    sizes = _stub_generation(monkeypatch, pool)
    monkeypatch.setattr(server, "_bridge_provider_for_tier", lambda *_a, **_k: "sonder_inference")
    server.parallel_generate_run("print one", variants=3, max_workers=3)
    assert sizes == [3]
    assert pool.asked == []


def test_fully_busy_pool_runs_one_candidate_at_a_time(monkeypatch):
    sizes = _stub_generation(monkeypatch, _Pool(0))
    out = server.parallel_generate_run("print one", variants=3, max_workers=3)
    assert sizes == [1]
    assert "workers=1, requested 3, clamped to available Ollama pool capacity 0" in out.splitlines()[0]
