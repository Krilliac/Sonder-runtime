"""Unit tests for chat model fitness policy and cached lookup."""
from __future__ import annotations

from sonder_runtime.application.chat.model_fitness import ChatModelFitness
from sonder_runtime.domain.chat_model_fitness import declared_context, unfit_reason

_TOY_SHOW = {
    "capabilities": ["completion"],
    "parameters": "num_ctx 256\nnum_predict 40",
    "model_info": {"gpt2.context_length": 256},
}
_GOOD_SHOW = {
    "capabilities": ["completion", "tools"],
    "model_info": {"llama.context_length": 131072},
}
_EMBED_SHOW = {"capabilities": ["embedding"], "model_info": {"bert.context_length": 8192}}
_SHOWS = {"astra-construct0": _TOY_SHOW, "good:7b": _GOOD_SHOW, "embedder": _EMBED_SHOW}


def test_unfit_reason_policy():
    assert unfit_reason(_GOOD_SHOW) == ""
    assert "context window 256" in unfit_reason(_TOY_SHOW)
    assert "embedding" in unfit_reason(_EMBED_SHOW)
    assert unfit_reason({"capabilities": ["tools"]}) == "no completion capability"
    assert unfit_reason({"parameters": "num_ctx 1024"}).startswith("context window 1024")
    assert unfit_reason({"model_info": {"x.context_length": 2048}}) == ""
    # nothing declared (older Ollama) or an unusable payload is not evidence
    assert unfit_reason({}) == "" and unfit_reason(None) == ""


def test_declared_context_picks_smallest_positive_value():
    assert declared_context({"parameters": "num_ctx 4096", "model_info": {"a.context_length": 131072}}) == 4096
    assert declared_context({"model_info": {"a.context_length": True, "b.context_length": 0}}) is None


def test_fitness_lookup_is_cached_and_fails_open():
    now = [0.0]
    probes = []

    def show(model):
        probes.append(model)
        if model == "down":
            raise RuntimeError("ollama unreachable")
        return _SHOWS[model]

    fitness = ChatModelFitness(show, clock=lambda: now[0])
    assert fitness.unfit_reason("") == ""
    assert "context window" in fitness.unfit_reason("astra-construct0")
    assert "context window" in fitness.unfit_reason("ASTRA-construct0")
    assert probes == ["astra-construct0"]
    assert fitness.unfit_reason("down") == ""        # failed probe reads as fit...
    assert fitness.unfit_reason("down") == ""
    assert probes.count("down") == 1                  # ...and is not hammered
    now[0] = 31.0
    fitness.unfit_reason("down")
    assert probes.count("down") == 2                  # retried after the short window
    now[0] = 400.0
    fitness.unfit_reason("astra-construct0")
    assert probes.count("astra-construct0") == 2      # positive entries expire too
