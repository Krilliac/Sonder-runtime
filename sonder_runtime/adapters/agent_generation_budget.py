"""Output headroom for bridged decisions without changing Ollama budgets."""
from __future__ import annotations

import os


def _limit(name: str) -> int:
    try:
        value = int(os.environ.get(name, "4096"))
    except (TypeError, ValueError):
        return 4096
    return min(value, 8192) if value > 0 else 4096


def decision_num_predict(provider, cloud=False, thinking_pinned=False) -> int:
    """Reserve reasoning headroom on Inference, including unknown pin state.

    A disabled/unknown thinking pin does not lower the cap: visible tool JSON
    also needs the headroom. Hosted callers retain their separate cloud budget.
    """
    if provider != "sonder_inference" or cloud:
        return 1200
    return _limit("SONDER_AGENT_NUM_PREDICT")


def json_num_predict(provider=None, cloud=False, thinking_pinned=False) -> int:
    """Planner/reviewer cap; Ollama keeps its historical 1,800 tokens."""
    if provider != "sonder_inference" or cloud:
        return 1800
    return _limit("SONDER_AUTOPILOT_JSON_NUM_PREDICT")
