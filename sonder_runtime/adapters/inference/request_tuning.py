"""Per-request tuning for Sonder Inference chat completions.

Two request-shaping decisions the gateway makes after the readiness check,
kept out of the gateway so they can be tested (and changed) on their own:

**Thinking.**  Qwen-style chat templates take ``enable_thinking`` through
``chat_template_kwargs``.  Sonder Inference ignored that field before its
request-path work, so forwarding it blindly would silently do nothing.  The
runtime forwards ``think`` only when the server says it honours it, read from
the (already cached) health document -- a ``thinking``,
``chat_template_kwargs`` or ``enable_thinking`` entry in a top-level or
``sonder`` ``features``/``capabilities`` list, or in any ``backends[]`` or
``models[]`` ``capabilities`` list -- or when the operator says so with
``SONDER_INFERENCE_THINKING=on``.  Otherwise the historical behaviour holds:
``think=True`` is refused and ``think=False`` is dropped.

**Sampling defaults.**  A GGUF may carry the model's recommended
temperature/top_p/top_k but not ``min_p``, so llama.cpp silently samples
with its own ``min_p`` of 0.05.  With ``SONDER_INFERENCE_SAMPLING_DEFAULTS=1``
the runtime fills the model family's recommended values for every sampling
field the caller did not set.  Off by default: the table changes decoding
for every tier bound to the provider, which has not been proven neutral for
all of them.  ``SONDER_INFERENCE_SAMPLING_TABLE`` may replace the built-in
table with a JSON list of families (same shape as :data:`BUILTIN_FAMILIES`).
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ...application.chat.provider_bridge import UnsupportedProviderFeature
from ...domain.common.errors import InvalidInput

ENV_THINKING = "SONDER_INFERENCE_THINKING"
ENV_SAMPLING_DEFAULTS = "SONDER_INFERENCE_SAMPLING_DEFAULTS"
ENV_SAMPLING_TABLE = "SONDER_INFERENCE_SAMPLING_TABLE"

THINKING_MARKERS = frozenset({"thinking", "chat_template_kwargs", "enable_thinking"})
THINKING_REFUSAL = (
    "model thinking is only available on Ollama tiers: this Sonder Inference "
    "server does not advertise thinking support (set %s=on if it honours "
    "chat_template_kwargs.enable_thinking)" % ENV_THINKING
)
# Request fields a family row may set, with the wire names the gateway uses.
SAMPLING_FIELDS = (
    "temperature", "top_p", "top_k", "min_p", "presence_penalty",
    "frequency_penalty", "repeat_penalty",
)
_INTEGER_FIELDS = frozenset({"top_k"})
_ON = frozenset({"1", "on", "true", "yes"})
_OFF = frozenset({"0", "off", "false", "no"})

# Qwen's published recommendations for Qwen3.x (thinking / non-thinking).
BUILTIN_FAMILIES: tuple[Mapping[str, object], ...] = (
    MappingProxyType({
        "family": "qwen3.x",
        "match": r"(?i)(?<![a-z0-9])qwen3(?:\.\d+)?(?!\d)",
        "exclude": r"(?i)coder",
        "template_default_thinking": True,
        "thinking": MappingProxyType({
            "temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 0.0,
        }),
        "non_thinking": MappingProxyType({
            "temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
            "presence_penalty": 1.5,
        }),
    }),
)

DECISION_SAMPLING: Mapping[str, Mapping[str, float | int]] = MappingProxyType({
    "thinking": MappingProxyType({"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0}),
    "non_thinking": MappingProxyType({"temperature": 0.7, "top_p": 0.8, "top_k": 20}),
})


def _flag(env: Mapping[str, str], name: str, *, default: str) -> str:
    raw = str(env.get(name, "") or "").strip().lower() or default
    if raw in _ON:
        return "on"
    if raw in _OFF:
        return "off"
    if raw == "auto":
        return "auto"
    raise InvalidInput("%s must be auto, on or off, got %r" % (name, raw))


def _names(value: object) -> set[str]:
    if isinstance(value, Mapping):
        return {str(item).strip().lower() for item, enabled in value.items() if enabled}
    if isinstance(value, (list, tuple, set, frozenset)):
        return {item.strip().lower() for item in value if isinstance(item, str)}
    return set()


def advertised_features(document: Mapping[str, object] | None) -> frozenset[str]:
    """Every feature/capability name the health document advertises."""
    if not isinstance(document, Mapping):
        return frozenset()
    names: set[str] = set()
    containers = [document]
    extension = document.get("sonder")
    if isinstance(extension, Mapping):
        containers.append(extension)
    for container in containers:
        names |= _names(container.get("features"))
        names |= _names(container.get("capabilities"))
    for key in ("backends", "models"):
        entries = document.get(key)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, Mapping):
                    names |= _names(entry.get("capabilities"))
    return frozenset(names)


def thinking_supported(document: Mapping[str, object] | None,
                       env: Mapping[str, str] | None = None) -> bool:
    """Whether ``think`` may be forwarded as ``enable_thinking``."""
    mode = _flag(os.environ if env is None else env, ENV_THINKING, default="auto")
    if mode != "auto":
        return mode == "on"
    return bool(advertised_features(document) & THINKING_MARKERS)


def apply_thinking(payload: dict, think: object, *, supported: bool) -> bool | None:
    """Put ``think`` on ``payload``; return the forwarded value or ``None``.

    Unsupported: ``True`` is refused (as before), ``False`` is dropped.
    """
    if think is None:
        return None
    if not isinstance(think, bool):
        raise InvalidInput("model option think must be a boolean")
    if not supported:
        if think:
            raise UnsupportedProviderFeature(THINKING_REFUSAL)
        return None
    kwargs = payload.get("chat_template_kwargs")
    kwargs = dict(kwargs) if isinstance(kwargs, Mapping) else {}
    kwargs["enable_thinking"] = think
    payload["chat_template_kwargs"] = kwargs
    return think


@dataclass(frozen=True)
class SamplingFamily:
    family: str
    match: re.Pattern
    exclude: re.Pattern | None
    template_default_thinking: bool
    thinking: Mapping[str, float | int]
    non_thinking: Mapping[str, float | int]

    def matches(self, model: str) -> bool:
        return bool(self.match.search(model)) and not (
            self.exclude is not None and self.exclude.search(model)
        )


def _row(value: object, where: str) -> Mapping[str, float | int]:
    if not isinstance(value, Mapping):
        raise InvalidInput("%s must be an object" % where)
    row: dict[str, float | int] = {}
    for key, number in value.items():
        if key not in SAMPLING_FIELDS:
            raise InvalidInput("%s names unknown sampling field %r" % (where, key))
        if type(number) is bool or not isinstance(number, (int, float)) or number != number:
            raise InvalidInput("%s.%s must be a number" % (where, key))
        row[key] = int(number) if key in _INTEGER_FIELDS else float(number)
    return MappingProxyType(row)


def _family(entry: object, index: int) -> SamplingFamily:
    where = "%s[%d]" % (ENV_SAMPLING_TABLE, index)
    if not isinstance(entry, Mapping):
        raise InvalidInput("%s must be an object" % where)
    name = entry.get("family")
    pattern = entry.get("match")
    exclude = entry.get("exclude")
    if not isinstance(name, str) or not name.strip() or not isinstance(pattern, str):
        raise InvalidInput("%s needs a family name and a match pattern" % where)
    try:
        compiled = re.compile(pattern)
        excluded = re.compile(exclude) if isinstance(exclude, str) and exclude else None
    except re.error as exc:
        raise InvalidInput("%s has an invalid pattern: %s" % (where, exc)) from exc
    default_thinking = entry.get("template_default_thinking", False)
    if not isinstance(default_thinking, bool):
        raise InvalidInput("%s.template_default_thinking must be a boolean" % where)
    return SamplingFamily(
        family=name.strip(), match=compiled, exclude=excluded,
        template_default_thinking=default_thinking,
        thinking=_row(entry.get("thinking", {}), where + ".thinking"),
        non_thinking=_row(entry.get("non_thinking", {}), where + ".non_thinking"),
    )


def sampling_families(env: Mapping[str, str] | None = None) -> tuple[SamplingFamily, ...]:
    """The configured family table (built-in unless overridden)."""
    source = os.environ if env is None else env
    raw = str(source.get(ENV_SAMPLING_TABLE, "") or "").strip()
    entries: object = BUILTIN_FAMILIES
    if raw:
        try:
            entries = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise InvalidInput("%s is not valid JSON" % ENV_SAMPLING_TABLE) from exc
        if not isinstance(entries, list):
            raise InvalidInput("%s must be a JSON list of families" % ENV_SAMPLING_TABLE)
    return tuple(_family(entry, index) for index, entry in enumerate(entries))


def sampling_defaults_enabled(env: Mapping[str, str] | None = None) -> bool:
    mode = _flag(os.environ if env is None else env, ENV_SAMPLING_DEFAULTS, default="off")
    if mode == "auto":
        raise InvalidInput("%s must be on or off" % ENV_SAMPLING_DEFAULTS)
    return mode == "on"


def apply_sampling_defaults(
    payload: dict, model: str, *, thinking: bool | None,
    env: Mapping[str, str] | None = None,
    profile: str | None = None,
) -> str | None:
    """Fill the family's recommended values for fields the caller left unset.

    ``thinking`` is the forwarded ``enable_thinking`` (``None``: the
    template's default applies).  Returns the family name applied, or
    ``None`` when the flag is off or no family matches.
    """
    if (profile != "decision" and not sampling_defaults_enabled(env)) \
            or not isinstance(model, str) or not model:
        return None
    if profile == "decision":
        row = DECISION_SAMPLING["thinking" if thinking else "non_thinking"]
        for key, value in row.items():
            payload.setdefault(key, value)
        return "decision"
    for family in sampling_families(env):
        if not family.matches(model):
            continue
        effective = family.template_default_thinking if thinking is None else thinking
        for key, value in (family.thinking if effective else family.non_thinking).items():
            payload.setdefault(key, value)
        return family.family
    return None


def default_model_hint(document: Mapping[str, object] | None) -> str | None:
    """The id of the health document's default model, for family matching."""
    models = document.get("models") if isinstance(document, Mapping) else None
    if not isinstance(models, list):
        return None
    for entry in models:
        if isinstance(entry, Mapping) and entry.get("default") is True:
            ident = entry.get("id")
            return ident if isinstance(ident, str) and ident else None
    return None




def tune_request(payload: dict, think: object, document: Mapping[str, object] | None,
                 env: Mapping[str, str] | None = None) -> None:
    """Apply the thinking decision, then the sampling defaults, to ``payload``."""
    supported = thinking_supported(document, env) if think is not None else False
    forwarded = apply_thinking(payload, think, supported=supported)
    profile = payload.pop("sampling_profile", None)
    model = payload.get("model")
    if model == "default":
        model = default_model_hint(document) or model
    apply_sampling_defaults(
        payload, str(model or ""), thinking=forwarded, env=env, profile=profile,
    )


__all__ = [
    "BUILTIN_FAMILIES",
    "DECISION_SAMPLING",
    "ENV_SAMPLING_DEFAULTS",
    "ENV_SAMPLING_TABLE",
    "ENV_THINKING",
    "THINKING_REFUSAL",
    "SamplingFamily",
    "advertised_features",
    "apply_sampling_defaults",
    "apply_thinking",
    "default_model_hint",
    "sampling_defaults_enabled",
    "sampling_families",
    "thinking_supported",
    "tune_request",
]
