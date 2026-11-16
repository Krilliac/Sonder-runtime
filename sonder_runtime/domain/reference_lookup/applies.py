"""Which models get automatic reference lookup.

Pure decision rule: callers supply the rule (built from the runtime policy
section) and the kill-switch state, and act on the typed result.  Names are
judged with the same normalisation and path-segment matching as the
tool-lane block list, so the two lists agree on what a model name means.  The
shipped floor (passed in) is always part of every rule: the policy section can
add entries but never remove one.  No I/O happens here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..tool_lane_models import (
    ToolLaneFloor,
    name_candidates,
    normalize_name,
    policy_candidates,
)

# The floor models are the tool-lane deny list's: the same prefixes (so every
# quantisation of the two source repositories, tagged or not) and names.  The
# floor is passed in; no model name is written here.

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
    """The floor unioned with the policy section."""

    enabled: bool
    names: frozenset[str]
    prefixes: tuple[str, ...]
    floor: ToolLaneFloor = ToolLaneFloor()

    @classmethod
    def from_policy(cls, section: Mapping[str, Any] | None, *, floor: ToolLaneFloor) -> AppliesRule:
        raw = section if isinstance(section, Mapping) else {}
        names = floor.names | frozenset(_clean_entries(raw.get("names")))
        prefixes = list(floor.prefixes)
        for prefix in _clean_entries(raw.get("prefixes")):
            if prefix not in prefixes:
                prefixes.append(prefix)
        return cls(enabled=raw.get("enabled", True) is not False,
                   names=names, prefixes=tuple(prefixes), floor=floor)

    @property
    def extended(self) -> bool:
        """True when the rule holds anything beyond the shipped floor."""
        return self.names != self.floor.names or self.prefixes != self.floor.prefixes


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
    original_candidates = policy_candidates(normalized)
    for candidate in name_candidates(normalized):
        if candidate in original_candidates and candidate in rule.names:
            return AppliesDecision(True, AppliesReason.MATCHED_NAME, candidate)
        for prefix in rule.prefixes:
            if candidate.startswith(prefix) and (
                prefix in rule.floor.prefixes or candidate in original_candidates
            ):
                return AppliesDecision(True, AppliesReason.MATCHED_PREFIX, prefix)
    return AppliesDecision(False, AppliesReason.NOT_CONFIGURED)
