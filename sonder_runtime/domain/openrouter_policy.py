"""Pure rules for the OpenRouter model provider.

OpenRouter (https://openrouter.ai) is a hosted, metered, OpenAI-compatible
router in front of many upstream inference hosts.  This module holds the parts
of that integration that must not depend on I/O or process state:

* the model id grammar (``vendor/model[:variant]``);
* the ``provider`` routing-preference object OpenRouter accepts on a chat
  request, its validation, and the privacy-first defaults this runtime sends
  unless the operator explicitly overrides them;
* normalisation of one ``/models`` catalog entry into the summary the CLI
  and MCP surfaces show (prices converted from USD per token to USD per
  million tokens).

Field names follow OpenRouter's API reference ("Provider Routing",
"List available models"); unknown keys are refused rather than forwarded so a
typo can never silently drop a privacy preference.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping

PROVIDER_ID = "openrouter"
# The model ids OpenRouter publishes: ``vendor/model`` with an optional
# ``:variant`` (``:free``, ``:nitro``, ``:beta`` ...).  A leading ``~`` marks
# OpenRouter's moving "latest" aliases.  Whitespace, quotes, query syntax and
# path traversal are never part of an id.
MODEL_ID = re.compile(
    r"~?[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
    r"(?::[A-Za-z0-9][A-Za-z0-9._-]{0,31})?\Z"
)
PROVIDER_SLUG = re.compile(r"[a-z0-9][a-z0-9._/-]{0,63}\Z")

# Sent on every request unless the operator overrides a key explicitly.
# This runtime ships source code and tool output in prompts, so the default
# is: no upstream that stores or trains on prompts, zero-data-retention
# endpoints only, and OpenRouter may still fail over between such endpoints.
SAFE_PROVIDER_DEFAULTS: Mapping[str, object] = {
    "data_collection": "deny",
    "zdr": True,
    "allow_fallbacks": True,
}

_BOOL_FIELDS = frozenset({
    "allow_fallbacks", "require_parameters", "zdr", "enforce_distillable_text",
})
_SLUG_LIST_FIELDS = frozenset({"order", "only", "ignore"})
QUANTIZATIONS = frozenset({
    "int4", "int8", "fp4", "mxfp4", "nvfp4", "fp6", "fp8", "mxfp8", "fp16",
    "bf16", "fp32", "unknown",
})
SORT_VALUES = frozenset({"price", "throughput", "latency"})
_PERCENTILES = frozenset({"p50", "p75", "p90", "p99"})
_MAX_PRICE_KEYS = frozenset({"prompt", "completion", "request", "image"})
PROVIDER_FIELDS = frozenset({
    *_BOOL_FIELDS, *_SLUG_LIST_FIELDS, "data_collection", "quantizations",
    "sort", "preferred_min_throughput", "preferred_max_latency", "max_price",
})
_MAX_LIST = 32


class OpenRouterPolicyError(ValueError):
    """An OpenRouter model id or provider preference is invalid."""


def is_model_id(value: object) -> bool:
    return isinstance(value, str) and MODEL_ID.fullmatch(value) is not None


def validate_model_id(value: object, where: str = "OpenRouter model id") -> str:
    """Return a stripped model id or raise with the expected grammar."""
    text = value.strip() if isinstance(value, str) else ""
    if not is_model_id(text):
        raise OpenRouterPolicyError(
            "%s must look like vendor/model or vendor/model:variant "
            "(for example anthropic/claude-sonnet-4 or "
            "meta-llama/llama-3.3-70b-instruct:free), got %r"
            % (where, value if isinstance(value, str) else type(value).__name__)
        )
    return text


def _number(value: object, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpenRouterPolicyError("%s must be a number" % where)
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise OpenRouterPolicyError("%s must be a finite, non-negative number" % where)
    return value  # type: ignore[return-value]


def _number_or_percentiles(value: object, where: str) -> object:
    if isinstance(value, Mapping):
        unknown = set(value) - _PERCENTILES
        if unknown or not value:
            raise OpenRouterPolicyError(
                "%s must be a number or an object with keys %s"
                % (where, ", ".join(sorted(_PERCENTILES)))
            )
        return {key: _number(item, "%s.%s" % (where, key)) for key, item in value.items()}
    return _number(value, where)


def _slug_list(value: object, where: str) -> list[str]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise OpenRouterPolicyError("%s must be a list of provider slugs" % where)
    if len(value) > _MAX_LIST:
        raise OpenRouterPolicyError("%s lists more than %d providers" % (where, _MAX_LIST))
    result: list[str] = []
    for item in value:
        slug = item.strip().lower() if isinstance(item, str) else ""
        if not PROVIDER_SLUG.fullmatch(slug):
            raise OpenRouterPolicyError(
                "%s entry %r is not a provider slug (e.g. deepinfra, fireworks)"
                % (where, item)
            )
        if slug not in result:
            result.append(slug)
    return result


def normalize_provider_preferences(
    raw: object, where: str = "OpenRouter provider preferences",
) -> dict[str, object]:
    """Validate one ``provider`` object; unknown keys are refused."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise OpenRouterPolicyError("%s must be a JSON object" % where)
    unknown = sorted(set(map(str, raw)) - PROVIDER_FIELDS)
    if unknown:
        raise OpenRouterPolicyError(
            "%s has unknown key(s) %s (known: %s)"
            % (where, ", ".join(unknown), ", ".join(sorted(PROVIDER_FIELDS)))
        )
    result: dict[str, object] = {}
    for key, value in raw.items():
        field = "%s.%s" % (where, key)
        if key in _BOOL_FIELDS:
            if type(value) is not bool:
                raise OpenRouterPolicyError("%s must be true or false" % field)
            result[key] = value
        elif key in _SLUG_LIST_FIELDS:
            result[key] = _slug_list(value, field)
        elif key == "data_collection":
            if value not in ("allow", "deny"):
                raise OpenRouterPolicyError("%s must be \"allow\" or \"deny\"" % field)
            result[key] = value
        elif key == "quantizations":
            if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
                raise OpenRouterPolicyError("%s must be a non-empty list" % field)
            bad = [item for item in value if item not in QUANTIZATIONS]
            if bad:
                raise OpenRouterPolicyError(
                    "%s has unknown value(s) %s" % (field, ", ".join(map(str, bad)))
                )
            result[key] = list(dict.fromkeys(value))
        elif key == "sort":
            if isinstance(value, str):
                if value not in SORT_VALUES:
                    raise OpenRouterPolicyError(
                        "%s must be one of %s" % (field, ", ".join(sorted(SORT_VALUES)))
                    )
                result[key] = value
            elif isinstance(value, Mapping):
                if set(value) - {"by", "partition"} or value.get("by") not in SORT_VALUES:
                    raise OpenRouterPolicyError(
                        "%s object needs \"by\" in %s and optional \"partition\""
                        % (field, ", ".join(sorted(SORT_VALUES)))
                    )
                partition = value.get("partition")
                if partition is not None and not isinstance(partition, str):
                    raise OpenRouterPolicyError("%s.partition must be a string" % field)
                result[key] = dict(value)
            else:
                raise OpenRouterPolicyError("%s must be a string or an object" % field)
        elif key in ("preferred_min_throughput", "preferred_max_latency"):
            result[key] = _number_or_percentiles(value, field)
        elif key == "max_price":
            if not isinstance(value, Mapping) or not value or set(value) - _MAX_PRICE_KEYS:
                raise OpenRouterPolicyError(
                    "%s must be an object with keys among %s"
                    % (field, ", ".join(sorted(_MAX_PRICE_KEYS)))
                )
            result[key] = {name: _number(item, "%s.%s" % (field, name))
                           for name, item in value.items()}
    return result


def effective_provider_preferences(
    default: Mapping[str, object] | None = None,
    tier: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Safe defaults, then the operator default, then the tier override.

    Each layer replaces whole keys; a later ``{"zdr": false}`` is the only
    way to turn the zero-data-retention default off, and it is explicit.
    """
    merged: dict[str, object] = dict(SAFE_PROVIDER_DEFAULTS)
    merged.update(dict(default or {}))
    merged.update(dict(tier or {}))
    return merged


def _price_per_million(value: object) -> float | None:
    """USD per token (OpenRouter publishes decimal strings) -> USD per 1M."""
    if isinstance(value, bool):
        return None
    try:
        per_token = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(per_token) or per_token < 0:
        return None  # "-1" marks router pseudo-models with variable pricing
    return round(per_token * 1_000_000, 6)


def model_summary(entry: object) -> dict[str, object] | None:
    """Project one ``/models`` entry; ``None`` when it has no usable id."""
    if not isinstance(entry, Mapping):
        return None
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    pricing = entry.get("pricing") if isinstance(entry.get("pricing"), Mapping) else {}
    raw_parameters = entry.get("supported_parameters")
    parameters = sorted({
        item for item in raw_parameters if isinstance(item, str)
    }) if isinstance(raw_parameters, (list, tuple)) else []
    context = entry.get("context_length")
    if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
        top = entry.get("top_provider")
        context = top.get("context_length") if isinstance(top, Mapping) else None
        if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
            context = None
    name = entry.get("name")
    return {
        "id": model_id.strip(),
        "name": name.strip() if isinstance(name, str) else "",
        "context_length": context,
        "prompt_usd_per_million": _price_per_million(pricing.get("prompt")),
        "completion_usd_per_million": _price_per_million(pricing.get("completion")),
        "supports_tools": "tools" in parameters,
        "supports_structured_outputs": (
            "structured_outputs" in parameters or "response_format" in parameters
        ),
    }


def filter_models(
    summaries: list[dict[str, object]], *, search: str = "", tools: bool = False,
) -> list[dict[str, object]]:
    """Case-insensitive substring search over id and name; optional tools filter."""
    needle = str(search or "").strip().casefold()
    result = []
    for item in summaries:
        if tools and not item.get("supports_tools"):
            continue
        if needle and needle not in str(item.get("id", "")).casefold() \
                and needle not in str(item.get("name", "")).casefold():
            continue
        result.append(item)
    return sorted(result, key=lambda item: str(item.get("id", "")))


__all__ = [
    "MODEL_ID",
    "OpenRouterPolicyError",
    "PROVIDER_FIELDS",
    "PROVIDER_ID",
    "SAFE_PROVIDER_DEFAULTS",
    "effective_provider_preferences",
    "filter_models",
    "is_model_id",
    "model_summary",
    "normalize_provider_preferences",
    "validate_model_id",
]
