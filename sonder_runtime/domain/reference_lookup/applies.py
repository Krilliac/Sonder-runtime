"""Which models get automatic reference lookup.

Pure decision rule: callers supply the rule (built from the runtime policy
section) and the kill-switch state, and act on the typed result.  Names are
judged with the same normalisation and last-path-segment matching as the
tool-lane block list, so the two lists agree on what a model name means.  The
built-in defaults are always part of every rule: the policy section can add
entries but never remove one.  No I/O happens here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..tool_lane_models import name_candidates, normalize_name

DEFAULT_PREFIXES = ("astra-cyber-",)
DEFAULT_NAMES = (
    normalize_name("hf.co/mradermacher/Qwen3.8-cyber-RedTeam-Surgical-Abliterated-GGUF:Q3_K_M"),
    normalize_name("hf.co/DuoNeural/Cyber-Ornith-1.5-9B-OBLITERATED-GGUF:Q8_0"),
)

SECTION_KEYS = frozenset({"enabled", "names", "prefixes"})
PATH_KEY = "path"


class AppliesReason(str, Enum):
    MATCHED_NAME = "matched_name"
    MATCHED_PREFIX = "matched_prefix"
    NOT_CONFIGURED = "not_configured"
    NO_MODEL = "no_model"
    DISABLED_BY_POLICY = "disabled_by_policy"
    DISABLED_BY_KILL_SWITCH = "disabled_by_kill_switch"


@dataclass(frozen=True)
class AppliesDecision:
    applies: bool
    reason: AppliesReason
    matched: str = ""


def default_section() -> dict[str, Any]:
    """The built-in policy section: lookup on, no extra names or prefixes."""
    return {"enabled": True, "names": [], "prefixes": []}


def _clean_entries(values: Any) -> list[str]:
    if values is None or isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        return []
    return [entry for entry in (normalize_name(value) for value in values) if entry]


@dataclass(frozen=True)
class AppliesRule:
    """Defaults unioned with the policy section."""

    enabled: bool
    names: frozenset[str]
    prefixes: tuple[str, ...]

    @classmethod
    def from_policy(cls, section: Mapping[str, Any] | None) -> AppliesRule:
        raw = section if isinstance(section, Mapping) else {}
        names = frozenset(DEFAULT_NAMES) | frozenset(_clean_entries(raw.get("names")))
        prefixes = list(DEFAULT_PREFIXES)
        for prefix in _clean_entries(raw.get("prefixes")):
            if prefix not in prefixes:
                prefixes.append(prefix)
        return cls(enabled=raw.get("enabled", True) is not False,
                   names=names, prefixes=tuple(prefixes))

    @property
    def extended(self) -> bool:
        """True when the rule holds anything beyond the built-in defaults."""
        return (self.names != frozenset(DEFAULT_NAMES)
                or self.prefixes != DEFAULT_PREFIXES)


def decide(model: Any, rule: AppliesRule, *, kill_switch_off: bool = False) -> AppliesDecision:
    """Whether automatic lookup applies to ``model``.

    ``kill_switch_off`` is True when the environment kill switch has turned the
    feature off.  A disabled policy section or the kill switch turns automatic
    lookup off for every model.
    """
    if kill_switch_off:
        return AppliesDecision(False, AppliesReason.DISABLED_BY_KILL_SWITCH)
    if not rule.enabled:
        return AppliesDecision(False, AppliesReason.DISABLED_BY_POLICY)
    normalized = normalize_name(model)
    if not normalized:
        return AppliesDecision(False, AppliesReason.NO_MODEL)
    for candidate in name_candidates(normalized):
        if candidate in rule.names:
            return AppliesDecision(True, AppliesReason.MATCHED_NAME, candidate)
        for prefix in rule.prefixes:
            if candidate.startswith(prefix):
                return AppliesDecision(True, AppliesReason.MATCHED_PREFIX, prefix)
    return AppliesDecision(False, AppliesReason.NOT_CONFIGURED)
