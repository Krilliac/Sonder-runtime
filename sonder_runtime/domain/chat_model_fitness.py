"""Whether a local model can serve chat, judged from one Ollama ``/api/show``.

Pure policy: no I/O, no clock.  The catalog (``/api/tags``) carries no
capabilities, so the picker and the request gate probe ``/api/show`` and apply
this.  A payload that says nothing leaves the model eligible (an older Ollama
omits ``capabilities``); only positive evidence excludes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sonder_runtime.domain.fanout_policy import nonchat_reason
from sonder_runtime.domain.model_capabilities import fanout_capabilities

MIN_CHAT_CONTEXT = 2048

_GENERATIVE = frozenset({"completion", "chat", "generate", "text-generation"})
_NUM_CTX = re.compile(r"^\s*num_ctx\s+(\d+)\s*$", re.MULTILINE)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    elif isinstance(value, str):
        try:
            number = int(value)
        except ValueError:
            return None
    else:
        return None
    return number if number > 0 else None


def declared_context(show: dict[str, object]) -> int | None:
    """The smallest context the model declares: ``*.context_length`` or ``num_ctx``."""
    values: list[int] = []
    info = show.get("model_info")
    if isinstance(info, dict):
        for name, value in info.items():
            if str(name).casefold().endswith("context_length"):
                parsed = _positive_int(value)
                if parsed is not None:
                    values.append(parsed)
    parameters = show.get("parameters")
    if isinstance(parameters, str):
        values.extend(int(match) for match in _NUM_CTX.findall(parameters))
    return min(values) if values else None


def unfit_reason(
    show: object,
    *,
    vision_only_model_families: Iterable[str] = (),
) -> str:
    """Why a model cannot serve chat, or ``""`` when it can (or nothing says).

    Excludes a declared capability set with no generative entry (embedding-only
    keeps the fanout wording) and a declared context window under
    ``MIN_CHAT_CONTEXT`` tokens, which is a toy or test model.
    """
    if not isinstance(show, dict):
        return ""
    reason = nonchat_reason(show, vision_only_model_families=vision_only_model_families)
    if reason:
        return reason
    capabilities = fanout_capabilities(show)
    if capabilities and not capabilities & _GENERATIVE:
        return "no completion capability"
    context = declared_context(show)
    if context is not None and context < MIN_CHAT_CONTEXT:
        return "context window %d is below the %d-token minimum" % (context, MIN_CHAT_CONTEXT)
    return ""


__all__ = ["MIN_CHAT_CONTEXT", "declared_context", "unfit_reason"]
