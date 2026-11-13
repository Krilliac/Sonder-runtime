"""Which installed models may drive tool-using lanes.

Some installed chat models are for by-name conversation only: they must never
select or call tools, and nothing they write may be executed.  This module is
the pure decision rule; callers supply the deny list (read from the runtime
policy) and act on the typed result.  No I/O happens here.

The list is add-only: the built-in defaults are always part of every deny list
built by :meth:`ToolLaneDenyList.from_policy`, so configuration can extend the
list but never shrink it.  When the policy file is unreadable the list is
marked unreadable and caller-named (free-form) models fail closed.

A tier bound to a provider other than Ollama is judged by the model that
provider actually serves (:func:`decide_served`).  A blocked model never
serves a tier, in any lane; when the served model cannot be determined the
tier is refused in tool-using lanes only.

Limit: this is a name list.  It cannot recognise a renamed copy of a blocked
model (``ollama cp astra-cyber-27b helper``), or an alias such as
``sonder:latest`` that an operator points at one; the weights behind a name
are not inspected.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

_WHITESPACE = re.compile(r"\s+")
_LATEST = ":latest"


class ModelLaneStatus(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    POLICY_UNREADABLE = "policy_unreadable"
    # A bridged tier whose served model is unknown (or the provider default).
    SERVED_UNKNOWN = "served_unknown"


@dataclass(frozen=True)
class ModelLaneDecision:
    status: ModelLaneStatus
    model: str
    matched: str = ""

    @property
    def denied(self) -> bool:
        return self.status is not ModelLaneStatus.ALLOWED


def normalize_name(name: Any) -> str:
    """Casefold, trim, collapse whitespace and drop one trailing ``:latest``."""
    text = _WHITESPACE.sub(" ", str(name if name is not None else "").strip()).casefold()
    if text.endswith(_LATEST):
        text = text[: -len(_LATEST)]
    return text


# The two Hugging Face sources end at ``-gguf`` so every quantisation tag and
# the untagged name are covered; the ``huggingface.co/`` spelling is listed
# too so either host form of the same source is caught.
_DENIED_SOURCES = (
    "mradermacher/Qwen3.8-cyber-RedTeam-Surgical-Abliterated-GGUF",
    "DuoNeural/Cyber-Ornith-1.5-9B-OBLITERATED-GGUF",
)
DEFAULT_DENIED_PREFIXES = ("astra-cyber-",) + tuple(
    normalize_name(host + source)
    for source in _DENIED_SOURCES
    for host in ("hf.co/", "huggingface.co/")
)
DEFAULT_DENIED_NAMES: tuple[str, ...] = ()
# Served-model ids that do not name a model: the provider picks one itself.
UNDETERMINED_SERVED_MODELS = frozenset({"", "default"})


def _clean_entries(values: Any) -> list[str]:
    if values is None or isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return []
    return [entry for entry in (normalize_name(value) for value in values) if entry]


@dataclass(frozen=True)
class ToolLaneDenyList:
    names: frozenset[str]
    prefixes: tuple[str, ...]
    readable: bool = True

    @classmethod
    def from_policy(
        cls, section: Mapping[str, Any] | None, *, readable: bool = True,
    ) -> "ToolLaneDenyList":
        """Defaults unioned with the policy section's ``names``/``prefixes``."""
        raw = section if isinstance(section, Mapping) else {}
        names = frozenset(DEFAULT_DENIED_NAMES) | frozenset(_clean_entries(raw.get("names")))
        prefixes = list(DEFAULT_DENIED_PREFIXES)
        for prefix in _clean_entries(raw.get("prefixes")):
            if prefix not in prefixes:
                prefixes.append(prefix)
        return cls(names=names, prefixes=tuple(prefixes), readable=bool(readable))

    @property
    def extended(self) -> bool:
        """True when the list holds anything beyond the built-in defaults."""
        return (
            self.names != frozenset(DEFAULT_DENIED_NAMES)
            or self.prefixes != DEFAULT_DENIED_PREFIXES
        )


def refusal_message(
    model: Any, status: ModelLaneStatus = ModelLaneStatus.DENIED,
) -> str:
    """The user-facing refusal for a model blocked from tool-using lanes."""
    if status is ModelLaneStatus.POLICY_UNREADABLE:
        return (
            "model '%s' cannot be used in tool-using lanes while the runtime "
            "policy file is unreadable; repair the file (`/runtime status` "
            "shows the error) or reset it with `/runtime reset`." % model
        )
    return (
        "model '%s' is blocked from tool-using lanes by runtime policy; "
        "it can be used for chat by name only." % model
    )


def served_refusal_message(
    tier: Any, provider: Any, verdict: ModelLaneDecision, *, fix: str = "", setting: str = "",
) -> str:
    """The refusal for a bridged tier judged by the model its provider serves.

    A blocked served model is refused in every lane, chat by tier included:
    a blocked model does not serve a tier.  ``setting`` names the provider
    setting that chooses the served model.  An undetermined served model is
    refused in tool-using lanes only; ``fix`` replaces the generic advice
    for it with the provider's own setting (the caller knows which one it is).
    """
    if verdict.status is ModelLaneStatus.SERVED_UNKNOWN:
        return (
            f"tier '{tier}' is served by {provider or 'a non-Ollama provider'} "
            "and the model it serves cannot be determined, so it cannot be "
            "used in tool-using lanes; "
            + (fix or "name the served model for this tier in that provider's configuration")
            + " (chat still works)."
        )
    change = (
        f"change the model {provider} serves for tier '{tier}' ({setting})" if setting
        else "change the model that provider serves for this tier in its configuration"
    )
    return (
        f"tier '{tier}' is served by {provider or 'a non-Ollama provider'} with model "
        f"'{verdict.model}', which is blocked by runtime policy, and a blocked model "
        f"does not serve a tier; ask for '{verdict.model}' by name for a text answer, "
        f"or {change}."
    )


def default_route_refusal_message(tier: Any, reason: str) -> str:
    """The refusal for the default route when a tier its escalation can reach is refused.

    The default route steps up from its start to a stronger configured local
    tier when an answer fails or is empty, so a lane that runs the reply
    judges every tier that escalation can reach; ``reason`` is that tier's
    own refusal, which names the setting that fixes it.  The other fix is to
    keep the default route on its start tier.
    """
    return (
        f"the default route can step up to tier '{tier}' when an answer fails or "
        f"is empty, and {reason} Alternatively set SONDER_MODEL_ESCALATION=0 to "
        "keep the default route on its first tier."
    )


def decide_served(model: Any, deny: ToolLaneDenyList) -> ModelLaneDecision:
    """Decide for the model a non-Ollama provider serves for a tier.

    An empty or ``default`` id leaves the choice to the provider, so the
    model is unknown (``SERVED_UNKNOWN``; the caller decides whether the
    provider's reply can still name it); anything else is judged as a
    tier-bound model.
    """
    raw = str(model if model is not None else "").strip()
    if normalize_name(raw) in UNDETERMINED_SERVED_MODELS:
        return ModelLaneDecision(ModelLaneStatus.SERVED_UNKNOWN, raw)
    return decide(raw, deny, free_form=False)


def allowed_bindings(
    bindings: Mapping[str, str], deny: ToolLaneDenyList, defaults: Mapping[str, str],
) -> dict[str, str]:
    """Copy tier bindings, replacing a denied model with that tier's default.

    A tier with no default (for example a hosted tier) is dropped instead of
    keeping a denied model.
    """
    result: dict[str, str] = {}
    for tier, model in bindings.items():
        if decide(model, deny, free_form=False).denied:
            if tier in defaults:
                result[tier] = defaults[tier]
            continue
        result[tier] = model
    return result


def decide_selector(
    selector: Any, bindings: Mapping[str, str], deny: ToolLaneDenyList,
) -> ModelLaneDecision:
    """Decide for a caller's tier-or-model selector without resolving it.

    The default route is always allowed, a bound tier is judged by the model it
    is bound to, and anything else is a caller-named (free-form) model.
    """
    text = str(selector if selector is not None else "").strip()
    key = text.lower()
    if key in ("", "sonder", "local"):
        return ModelLaneDecision(ModelLaneStatus.ALLOWED, text)
    if key in bindings:
        return decide(bindings[key], deny, free_form=False)
    return decide(text, deny, free_form=True)


def name_candidates(normalized: str) -> tuple[str, ...]:
    """The names a normalized model name is matched as: itself, then its last path segment."""
    tail = normalized.rsplit("/", 1)[-1]
    return (normalized,) if tail == normalized else (normalized, tail)


_candidates = name_candidates  # the original private name, kept for existing callers


def decide(model: Any, deny: ToolLaneDenyList, *, free_form: bool) -> ModelLaneDecision:
    """Decide whether ``model`` may be used in a tool-using lane.

    Exact names and prefixes are matched against the normalized name and its
    last path segment.  With an unreadable policy a caller-named (``free_form``)
    model fails closed; a tier-bound model is still checked against the lists.
    """
    raw = str(model if model is not None else "").strip()
    normalized = normalize_name(raw)
    if not normalized:
        return ModelLaneDecision(ModelLaneStatus.ALLOWED, raw)
    for candidate in _candidates(normalized):
        if candidate in deny.names:
            return ModelLaneDecision(ModelLaneStatus.DENIED, raw, candidate)
        for prefix in deny.prefixes:
            if candidate.startswith(prefix):
                return ModelLaneDecision(ModelLaneStatus.DENIED, raw, prefix)
    if free_form and not deny.readable:
        return ModelLaneDecision(ModelLaneStatus.POLICY_UNREADABLE, raw)
    return ModelLaneDecision(ModelLaneStatus.ALLOWED, raw)
