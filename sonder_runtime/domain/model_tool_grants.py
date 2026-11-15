"""Per-model tool grants for restricted profiles: the pure rules (no I/O).

A model profile in the model tool policy (``adapters/model_tool_policy``) that
lists ``tools`` and ``lanes`` is a *restricted* profile: a model it matches
may drive those tools, and only in those lanes.  Everything else about it is
unchanged: the model is still on the tool-lane deny list, so every lane that
does not ask this module stays refused (``tool_lane_models.decide``).  Only the
two lanes in :data:`RESTRICTED_LANES` consult :func:`decide_tools`.

Three rules live here, as code rather than lists:

* :func:`restricted_tool_refusal` - the safety predicate.  A tool on a
  restricted profile is kept only when its risk grade is ``safe``, it reaches
  no network, it reads or changes no memory or learning state, it is not a
  system-operator tool, it starts no program, and the restricted agent lane can
  actually dispatch it.  The caller supplies the facts it cannot know here
  (:class:`ToolFacts`: risk grade, operator and execution membership,
  dispatchability); the name-based parts are decided here.
* :func:`decide_tools` - which status a model has: ``ALLOWED`` (not listed),
  ``RESTRICTED`` (listed only by restricted profiles; the granted tools and
  lanes are the intersection over every matching profile), ``DENIED``,
  ``POLICY_UNREADABLE``, ``SERVED_UNKNOWN``, ``CONFIG_UNAVAILABLE`` or
  ``OVERLAY_UNUSABLE``.  A
  profile with no tools left after the predicate is a plain deny entry, so a
  model whose profiles grant nothing is ``DENIED`` exactly as before.
* :func:`secret_glob_match` - whether a path (relative to the project root)
  matches one of the configured secret globs.

Nothing here reads a file, the environment or a clock.
"""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .tool_lane_models import (
    ModelLaneStatus,
    ToolLaneDenyList,
    decide,
    decide_listed,
    decide_served,
)
from .tools.builtin_traits import _NETWORK as _BUILTIN_NETWORK

# The only lanes a restricted profile can open.  Every other lane treats a
# restricted model as denied because it never asks this module.
AGENT_LANE = "agent"
WORKBENCH_AGENT_LANE = "workbench_agent"
RESTRICTED_LANES = frozenset({AGENT_LANE, WORKBENCH_AGENT_LANE})

# Name fragments of tools that read or change memory, learned lessons or
# outcome records.  A restricted model may not read what Sonder remembers
# about its users, nor teach Sonder anything.
_MEMORY_MARKERS = (
    "memory", "learn", "remember", "forget", "record_outcome", "apply_learned", "lesson",
)
# Name fragments of tools that reach the network (beyond the built-in set).
_NETWORK_MARKERS = ("weather", "location")


class ModelToolStatus(str, Enum):
    ALLOWED = "allowed"
    RESTRICTED = "restricted"
    DENIED = "denied"
    POLICY_UNREADABLE = "policy_unreadable"
    SERVED_UNKNOWN = "served_unknown"
    CONFIG_UNAVAILABLE = "config_unavailable"
    OVERLAY_UNUSABLE = "overlay_unusable"


@dataclass(frozen=True)
class ToolFacts:
    """What the host knows about one tool, for the safety predicate.

    ``risk`` is the permission grade (``permission_modes.risk_of``);
    ``operator`` is membership of the agent's system-operator / excluded
    sets; ``execution`` is membership of the host's execution-tool set;
    ``dispatchable`` is whether the restricted agent lane (read-only, bound to
    a project) can dispatch it at all.
    """

    name: str
    risk: str
    operator: bool
    execution: bool
    dispatchable: bool


def restricted_tool_refusal(facts: ToolFacts) -> str:
    """Why ``facts.name`` may not be granted to a restricted model, or ``""``.

    Every condition must hold; the first that fails is named.  The order only
    chooses the message.
    """
    name = str(facts.name or "").strip()
    if not name:
        return "the tool name is empty"
    if facts.risk != "safe":
        return "its risk grade is %r, not 'safe'" % (facts.risk,)
    if name in _BUILTIN_NETWORK or name.startswith("web_") or any(
        marker in name for marker in _NETWORK_MARKERS
    ):
        return "it reaches the network"
    if any(marker in name for marker in _MEMORY_MARKERS):
        return "it reads or changes memory or learning state"
    if facts.operator or name.startswith("admin_"):
        return "it is a system-operator tool"
    if (
        facts.execution
        or name.startswith(("run_", "build_"))
        or name.endswith("_run")
        or "_run_" in name
    ):
        return "it starts a program"
    if not facts.dispatchable:
        return (
            "the restricted agent lane cannot dispatch it (it is not a read-only, "
            "project-confined agent tool)"
        )
    return ""


@dataclass(frozen=True)
class ToolGrant:
    """One restricted profile after the predicate: who it matches and what it grants.

    ``names`` and ``prefixes`` are normalised.  ``tools`` holds only tools that
    passed the predicate (and the ceiling); ``lanes`` only names from
    :data:`RESTRICTED_LANES`.
    """

    profile_id: str
    names: frozenset[str]
    prefixes: tuple[str, ...]
    tools: frozenset[str]
    lanes: frozenset[str]


@dataclass(frozen=True)
class ModelToolDecision:
    status: ModelToolStatus
    model: str
    matched: str = ""
    profiles: tuple[str, ...] = ()
    tools: frozenset[str] = frozenset()
    lanes: frozenset[str] = frozenset()

    @property
    def denied(self) -> bool:
        """True for every status but ``ALLOWED`` (a restricted model is denied by default)."""
        return self.status is not ModelToolStatus.ALLOWED

    def permits(self, lane: str) -> bool:
        """Whether this decision opens ``lane`` with at least one tool."""
        return (
            self.status is ModelToolStatus.RESTRICTED
            and lane in RESTRICTED_LANES
            and lane in self.lanes
            and bool(self.tools)
        )


def _matches(model: Any, grant: ToolGrant, deny: ToolLaneDenyList) -> bool:
    """Whether ``grant`` lists ``model``, matched exactly as the deny list matches it."""
    listed = ToolLaneDenyList(
        names=grant.names, prefixes=grant.prefixes, readable=True, floor=deny.floor,
    )
    return decide_listed(model, listed, free_form=False).status is ModelLaneStatus.DENIED


def decide_tools(
    model: Any,
    deny: ToolLaneDenyList,
    grants: Sequence[ToolGrant],
    deny_only: ToolLaneDenyList,
    *,
    free_form: bool,
) -> ModelToolDecision:
    """Decide which tools, if any, ``model`` may drive.

    ``deny`` is the full deny list in force (every profile, the overlay and
    the legacy policy section).  ``grants`` are the restricted profiles after
    the predicate; ``deny_only`` lists every entry that denies outright (a
    profile with no tools left, the legacy section, an explicit deny).  A model
    is ``RESTRICTED`` only when the deny list matches it, ``deny_only`` does
    not, at least one grant does, and the intersection of the matching grants'
    tools is not empty; it is ``DENIED`` otherwise.  An unreadable policy file
    turns what would be ``RESTRICTED`` into ``POLICY_UNREADABLE`` (the legacy
    section cannot be checked).  The statuses the deny list itself returns
    (``CONFIG_UNAVAILABLE``, ``OVERLAY_UNUSABLE``, ``POLICY_UNREADABLE``) pass
    through unchanged.
    """
    verdict = decide(model, deny, free_form=free_form)
    if verdict.status is ModelLaneStatus.ALLOWED:
        return ModelToolDecision(ModelToolStatus.ALLOWED, verdict.model)
    if verdict.status is not ModelLaneStatus.DENIED:
        return ModelToolDecision(ModelToolStatus(verdict.status.value), verdict.model, verdict.matched)
    refused = ModelToolDecision(ModelToolStatus.DENIED, verdict.model, verdict.matched)
    if decide_listed(model, deny_only, free_form=False).denied:
        return refused
    matching = [grant for grant in grants if _matches(model, grant, deny)]
    if not matching:
        return refused
    if not deny.readable:
        return ModelToolDecision(ModelToolStatus.POLICY_UNREADABLE, verdict.model, verdict.matched)
    tools = frozenset.intersection(*(frozenset(grant.tools) for grant in matching))
    lanes = frozenset.intersection(*(frozenset(grant.lanes) for grant in matching)) & RESTRICTED_LANES
    if not tools:
        return refused
    return ModelToolDecision(
        ModelToolStatus.RESTRICTED, verdict.model, verdict.matched,
        profiles=tuple(grant.profile_id for grant in matching), tools=tools, lanes=lanes,
    )


def decide_tools_served(
    model: Any, deny: ToolLaneDenyList,
) -> ModelToolDecision:
    """The decision for a model a non-Ollama provider serves for a tier.

    A restricted model never serves a tier: a listed served model is
    ``DENIED`` whatever its profiles grant, an undetermined one is
    ``SERVED_UNKNOWN``.
    """
    verdict = decide_served(model, deny)
    if verdict.status is ModelLaneStatus.ALLOWED:
        return ModelToolDecision(ModelToolStatus.ALLOWED, verdict.model)
    return ModelToolDecision(ModelToolStatus(verdict.status.value), verdict.model, verdict.matched)


def secret_glob_match(relative: str, globs: Iterable[str]) -> str:
    """The first glob in ``globs`` that ``relative`` matches, or ``""``.

    ``relative`` is a path relative to the project root (either separator).
    Matching is case-insensitive.  A glob with no ``/`` matches any single path
    component (``.env``, ``*.pem``, ``id_rsa*``); a glob with ``/`` matches any
    tail of the path, with or without a leading or trailing ``/`` (so
    ``.ssh/**`` and ``**/secrets/**`` match at any depth, the directory itself
    included).  ``*`` crosses ``/`` (``fnmatch`` semantics), which can only
    widen a match.
    """
    text = str(relative or "").replace("\\", "/").casefold()
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return ""
    tails = ["/".join(parts[index:]) for index in range(len(parts))]
    candidates = set()
    for tail in tails:
        candidates.update({tail, "/" + tail, tail + "/", "/" + tail + "/"})
    for pattern in globs:
        folded = str(pattern or "").casefold()
        if not folded:
            continue
        if "/" not in folded and any(fnmatch.fnmatchcase(part, folded) for part in parts):
            return str(pattern)
        if any(fnmatch.fnmatchcase(candidate, folded) for candidate in candidates):
            return str(pattern)
    return ""


__all__ = [
    "AGENT_LANE", "WORKBENCH_AGENT_LANE", "RESTRICTED_LANES",
    "ModelToolStatus", "ToolFacts", "ToolGrant", "ModelToolDecision",
    "restricted_tool_refusal", "decide_tools", "decide_tools_served", "secret_glob_match",
]
