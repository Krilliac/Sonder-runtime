"""Pure routing policy for HTTP entry points that bypass slash/tool dispatch.

Two served spellings used to reach work without the gate their catalogued
spelling meets: the SPEC-5 thin handlers (``/v1/recall``, ``/v1/outcome``),
which run the memory service against the shared corpus, and natural-language
control intents ("run it", "practice 20 tasks"), which ran ``/run`` and
``/train`` effects directly.  This module maps each to the spelling whose
gate must apply; the HTTP adapter supplies the gates themselves.  It has no
dependency on the server composition root.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

# The catalogued tool each thin handler performs, so its HTTP spelling meets
# the same per-tool account check as ``/<tool>``.
THIN_HANDLER_TOOLS: Mapping[str, str] = {
    "/v1/recall": "recall",
    "/v1/outcome": "record_outcome",
}


def thin_handler_refusal(
    path: str,
    context: Mapping[str, Any],
    *,
    memory_refusal: Callable[[str, Mapping[str, Any]], str],
    admin_authorized: Callable[[Mapping[str, Any]], bool],
) -> str:
    """Return "" to dispatch the thin handler at ``path``, else the refusal.

    A mapped handler meets its tool's account check; an unmapped one has no
    per-tool policy to apply, so it requires administrator authority.
    """
    tool = THIN_HANDLER_TOOLS.get(path)
    if tool is not None:
        return memory_refusal(tool, context)
    if not admin_authorized(context):
        return "administrator authorization is required"
    return ""


def intent_slash_lines(intent: Mapping[str, Any]) -> list[str]:
    """The slash commands equivalent to a classified natural-language intent.

    Order matches the historical effect order: trace, strict, run, train.
    """
    lines: list[str] = []
    if "trace" in intent:
        lines.append("/trace %s" % ("on" if intent["trace"] else "off"))
    if "strict" in intent:
        lines.append("/strict %s" % ("on" if intent["strict"] else "off"))
    if intent.get("run"):
        lines.append("/run")
    if "train" in intent:
        lines.append("/train %d" % int(intent["train"]))
    return lines


__all__ = ["THIN_HANDLER_TOOLS", "intent_slash_lines", "thin_handler_refusal"]
