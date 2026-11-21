"""Per-tier Ollama request options (``SONDER_OLLAMA_TIER_OPTIONS``).

One option is supported: ``num_ctx``, the context window a tier's Ollama
requests ask for.  When set it replaces the automatic, model-aware window for
that tier (an explicit per-request context size still wins), and it is the
context length context-fit routing measures an Ollama tier against.

The value is a JSON object ``{tier: {option: integer}}``, for example
``{"reasoning": {"num_ctx": 65536}, "code": {"num_ctx": 32768}}``.  Unset,
empty or invalid configuration changes nothing: every request is sent exactly
as before, and an invalid value is reported (``parse`` returns the error, and
``runtime_policy_status`` shows it) instead of failing a turn.

Pure configuration parsing: the environment is read on every call (never
cached), and nothing here opens a file or a socket.
"""
from __future__ import annotations

import json
import os
from typing import Mapping

ENV_TIER_OPTIONS = "SONDER_OLLAMA_TIER_OPTIONS"
# Mirrors ``domain.runtime_policy.rules.LOCAL_TIERS`` (platform may not import
# the domain); a test pins the two together.
TIERS = ("fast", "code", "general", "reasoning", "vision")
OPTION_BOUNDS = {
    "num_ctx": (512, 1_048_576),
}


def parse(raw: str | None) -> tuple[dict[str, dict[str, int]], str]:
    """``({tier: {option: value}}, error)``; an error keeps nothing."""
    text = str(raw or "").strip()
    if not text:
        return {}, ""
    try:
        document = json.loads(text)
    except ValueError:
        return {}, "%s is not a JSON object" % ENV_TIER_OPTIONS
    if not isinstance(document, dict):
        return {}, "%s is not a JSON object" % ENV_TIER_OPTIONS
    result: dict[str, dict[str, int]] = {}
    for tier, options in document.items():
        name = str(tier or "").strip().lower()
        if name not in TIERS:
            return {}, "%s names unknown tier %r (tiers: %s)" % (
                ENV_TIER_OPTIONS, tier, ", ".join(TIERS),
            )
        if not isinstance(options, dict):
            return {}, "%s tier %s must map option names to integers" % (ENV_TIER_OPTIONS, name)
        values: dict[str, int] = {}
        for option, value in options.items():
            bounds = OPTION_BOUNDS.get(option)
            if bounds is None:
                return {}, "%s tier %s: unknown option %r (options: %s)" % (
                    ENV_TIER_OPTIONS, name, option, ", ".join(OPTION_BOUNDS),
                )
            if type(value) is not int or not bounds[0] <= value <= bounds[1]:
                return {}, "%s tier %s: %s must be an integer in [%d, %d]" % (
                    ENV_TIER_OPTIONS, name, option, bounds[0], bounds[1],
                )
            values[option] = value
        if values:
            result[name] = values
    return result, ""


def configured(environ: Mapping[str, str] | None = None) -> tuple[dict[str, dict[str, int]], str]:
    """The tier options in force (read on every call, never cached)."""
    values = os.environ if environ is None else environ
    return parse(values.get(ENV_TIER_OPTIONS))


def tier_options(tier: object, environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """The configured options for ``tier`` (``{}`` when none or invalid)."""
    options, _error = configured(environ)
    return dict(options.get(str(tier or "").strip().lower(), {}))


def tier_num_ctx(tier: object, environ: Mapping[str, str] | None = None) -> int | None:
    """The tier's configured ``num_ctx``, or ``None`` to keep the automatic window."""
    return tier_options(tier, environ).get("num_ctx")


__all__ = [
    "ENV_TIER_OPTIONS",
    "OPTION_BOUNDS",
    "TIERS",
    "configured",
    "parse",
    "tier_num_ctx",
    "tier_options",
]
