"""Regression pins: provider-bound agent turns never probe Ollama context and partial pool pages skip status merge."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference.ollama_pool import _MAX_STATUS_SERIALIZED_BYTES
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.bootstrap import pool_wiring


@pytest.mark.parametrize("provider", ["sonder_inference", "openai_compatible"])
def test_provider_bound_agent_turn_never_probes_ollama_context(monkeypatch, provider):
    import server

    class Gateway:
        def generate(self, request, context):
            return ModelResponse('{"final":"Hello there."}', "bound-model", request.tier)

    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform(provider), model_gateway=Gateway())
    help_kwargs = []
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_application", lambda: graph)
    monkeypatch.setattr(server, "_serve_target", lambda *_args: ("same-model", False, False, "code"))
    monkeypatch.setattr(server, "_cloud_allowed_policy", lambda _env: True)
    monkeypatch.setattr(server, "_agent_tool_help", lambda **kwargs: help_kwargs.append(kwargs) or "")
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "host-profile")
    monkeypatch.setattr(server, "_post_model", lambda *_a, **_k: pytest.fail("agent sent to Ollama"))
    monkeypatch.setattr(server, "_auto_model_context", lambda *_a: pytest.fail("agent probed Ollama"))
    server._agent_impl(
        "Say hello", tier="code", max_steps=1, allow_web=False,
        auto_checklist=False, require_file_evidence=False, system="request-system",
    )
    assert help_kwargs and all(kw.get("num_ctx") == 0 for kw in help_kwargs)


class _Pool:
    def __init__(self):
        self.summary_calls = 0

    def pool_capacity_summary(self):
        self.summary_calls += 1
        return {"workers": 99}


def _page(workers, worker_count, **extra):
    return {"workers": list(workers), "worker_count": worker_count, "serialized_bytes": 0, **extra}


def test_merge_leaves_partial_page_unmerged_and_complete_page_merged():
    pool = _Pool()
    partial_omitted_complete = _page([{"origin": "a"}], 3)
    assert pool_wiring.merge_pool_unified_status(partial_omitted_complete, pool, None) is partial_omitted_complete
    assert pool.summary_calls == 0

    partial_cursor_end = _page([{"origin": "c"}], 3, complete=True)
    assert pool_wiring.merge_pool_unified_status(partial_cursor_end, pool, None) is partial_cursor_end
    assert pool.summary_calls == 0

    partial_flag_false = _page([{"origin": "a"}], 1, complete=False)
    assert pool_wiring.merge_pool_unified_status(partial_flag_false, pool, None) is partial_flag_false
    assert pool.summary_calls == 0

    full = _page([{"origin": "a"}], 1, complete=True)
    merged = pool_wiring.merge_pool_unified_status(full, pool, None)
    assert merged["capacity_summary"] == {"workers": 99}
    assert pool.summary_calls == 1


def test_merge_drops_the_merge_past_the_status_byte_cap():
    class _Big(_Pool):
        def pool_capacity_summary(self):
            return {"blob": "x" * (_MAX_STATUS_SERIALIZED_BYTES + 1)}

    page = _page([{"origin": "a"}], 1)
    result = pool_wiring.merge_pool_unified_status(page, _Big(), None)
    assert result is page
    assert "capacity_summary" not in result
