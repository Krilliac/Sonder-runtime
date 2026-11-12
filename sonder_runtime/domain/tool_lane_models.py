"""Which installed models may drive tool-using lanes.

Some installed chat models are for by-name conversation only: they must never
select or call tools, and nothing they write may be executed.  This module is
the pure decision rule; callers supply the deny list (read from the runtime
policy) and act on the typed result.  No I/O happens here.

The list is add-only: the built-in defaults are always part of every deny list
built by :meth:`ToolLaneDenyList.from_policy`, so configuration can extend the
list but never shrink it.  When the policy file is unreadable the list is
marked unreadable and caller-named (free-form) models fail closed.
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


DEFAULT_DENIED_PREFIXES = ("astra-cyber-",)
DEFAULT_DENIED_NAMES = (
    normalize_name("hf.co/mradermacher/Qwen3.8-cyber-RedTeam-Surgical-Abliterated-GGUF:Q3_K_M"),
    normalize_name("hf.co/DuoNeural/Cyber-Ornith-1.5-9B-OBLITERATED-GGUF:Q8_0"),
)


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
            "policy file is unreadable." % model
        )
    return (
        "model '%s' is blocked from tool-using lanes by runtime policy; "
        "it can be used for chat by name only." % model
    )


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


def _candidates(normalized: str) -> tuple[str, ...]:
    tail = normalized.rsplit("/", 1)[-1]
    return (normalized,) if tail == normalized else (normalized, tail)


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
