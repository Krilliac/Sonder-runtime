"""Pure thinking controls for hosted and local reasoning models.

Hosted models differ in whether their thinking mode can be requested,
bounded or disabled; local reasoning models need prediction headroom for
their thoughts plus the answer. This module owns the per-model hosted
controls, the narrow allow-list of models that accept ``think=false``, the
recognizer for Ollama's refusal of the optional ``think`` switch and the
local thinking budget. Moved from ``server.py`` in the WP1
Three-Hundred-Twelfth Slice with its behaviour byte-for-byte intact.
"""
from __future__ import annotations

import re


THINK_OPTION_UNSUPPORTED_RE = re.compile(
    r"\b(?:think|thinking)\b.*\b(?:unsupported|not\s+supported)\b"
    r"|\b(?:unsupported|does\s+not\s+support|not\s+supported)\b"
    r"(?:\s+\w+){0,3}\s+\b(?:think|thinking)\b",
    re.IGNORECASE,
)


def think_option_unsupported(detail) -> bool:
    """Whether Ollama explicitly refused only the optional ``think`` control."""
    return bool(THINK_OPTION_UNSUPPORTED_RE.search(str(detail or "")))


def cloud_can_disable_thinking(model) -> bool:
    """Whether the hosted model is known to accept ``think=false``.

    Keep this allow-list deliberately narrow. Some hosted reasoners require
    their thinking mode, while GLM 5.2 and Kimi K2.7 Code have both been
    observed accepting an explicit false value through Ollama's cloud API.
    """
    name = str(model or "").strip().casefold()
    return name.startswith(("glm-5.2:", "kimi-k2.7-code:"))


LOCAL_THINKING_MIN_NUM_PREDICT = 4096


def with_local_thinking_budget(payload, minimum=LOCAL_THINKING_MIN_NUM_PREDICT):
    """Return ``payload`` with room for a local model's thinking plus its answer.

    The local mirror of _ensure_cloud_prediction_budget. Returns a copy so a
    caller's dict is never mutated; an unset or already-generous num_predict is
    left alone, and 0/-1 (unlimited) is not a small budget.
    """
    options = payload.get("options")
    if not isinstance(options, dict):
        return dict(payload)
    requested = options.get("num_predict")
    if not isinstance(requested, int) or requested <= 0 or requested >= minimum:
        return dict(payload)
    payload = dict(payload)
    payload["options"] = dict(options, num_predict=minimum)
    return payload


# Some local community models serialize deliberation into ordinary ``content``
# rather than Ollama's separate ``message.thinking`` field.  That field is
# governed by explicit reasoning exposure policy; a leading closed tag must
# not become an accidental bypass of the same boundary.
def apply_cloud_thinking_policy(payload, model, *, compact=False, ensure_prediction_budget):
    """Apply hosted-model thinking controls without changing custom models.

    Tool-using agent turns need only a small JSON decision.  Keep their
    reasoning bounded so a hosted model cannot consume the whole prediction
    budget before returning that decision.  Ordinary offloads retain the
    quality-oriented policy below.

    ``ensure_prediction_budget(payload)`` raises the prediction budget for
    an ordinary hosted offload; it is injected so the root delegate that
    tests pin keeps its identity.
    """
    name = str(model or "").strip().casefold()
    if name.startswith("kimi-k3:"):
        # K3 is a native-thinking model; do not assume its hosted endpoint
        # supports disabling thought. Request it explicitly so hosted defaults
        # cannot drift. Compact agent mode keeps the caller's bounded budget;
        # ordinary offloads retain headroom for thinking plus final content.
        payload["think"] = True
        if not compact:
            ensure_prediction_budget(payload)
    elif name.startswith("glm-5.2:"):
        # GLM-5.2 accepts an explicit false value.  Even its "low" reasoning
        # mode can consume the entire shared prediction budget without
        # emitting the tiny JSON/native tool decision an agent turn needs.
        # Ordinary offloads retain the quality-oriented high setting.
        payload["think"] = False if compact else "high"
        if not compact:
            ensure_prediction_budget(payload)
    elif name.startswith("kimi-k2.7-code:"):
        # Code review benefits materially from the model's native reasoning
        # mode; the hosted API returns final content separately, so callers do
        # not receive or depend on the private thinking stream.
        payload["think"] = False if compact else True
        if not compact:
            ensure_prediction_budget(payload)
    elif name.startswith("gpt-oss:"):
        payload["think"] = "low"
