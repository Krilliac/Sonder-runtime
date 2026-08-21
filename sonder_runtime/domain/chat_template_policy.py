"""Provider-neutral policy for model chat-template request options.

Some local OpenAI-compatible servers expose template controls through a
``chat_template_kwargs`` object.  Keeping the normalization here prevents
transport adapters from accidentally emitting a top-level option that the
template never reads.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_EFFORT_ALIASES = {
    "none": "off",
    "disabled": "off",
}
_EFFORTS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh"})


class ChatTemplateOptionsError(ValueError):
    """Raised when chat-template options cannot be represented safely."""


def normalize_chat_template_options(options: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return OpenAI-compatible template kwargs with a stable shape.

    ``reasoning_effort`` is accepted as a convenience on the internal model
    request, then moved into the nested object expected by Qwen-style
    templates.  A conflicting top-level and nested value is rejected instead
    of silently choosing one.  Unrelated template kwargs are preserved so the
    policy remains provider-neutral.
    """
    if options is None:
        return {}
    if not isinstance(options, Mapping):
        raise ChatTemplateOptionsError("model options must be an object")

    raw_kwargs = options.get("chat_template_kwargs", {})
    if raw_kwargs is None:
        raw_kwargs = {}
    if not isinstance(raw_kwargs, Mapping):
        raise ChatTemplateOptionsError("chat_template_kwargs must be an object")
    kwargs = dict(raw_kwargs)

    top_level = options.get("reasoning_effort")
    nested = kwargs.get("reasoning_effort")
    if top_level is not None and nested is not None:
        if _canonical_effort(top_level) != _canonical_effort(nested):
            raise ChatTemplateOptionsError(
                "top-level and chat_template_kwargs reasoning_effort disagree"
            )
    selected = nested if nested is not None else top_level
    if selected is not None:
        kwargs["reasoning_effort"] = _canonical_effort(selected)
    return kwargs


def _canonical_effort(value: Any) -> str:
    if not isinstance(value, str):
        raise ChatTemplateOptionsError("reasoning_effort must be a string")
    effort = value.strip().lower()
    effort = _EFFORT_ALIASES.get(effort, effort)
    if effort not in _EFFORTS:
        raise ChatTemplateOptionsError(
            "reasoning_effort must be one of off, minimal, low, medium, high, xhigh"
        )
    return effort


__all__ = ["ChatTemplateOptionsError", "normalize_chat_template_options"]
