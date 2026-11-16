"""Restricted model profiles: which granted tools survive, and the status of every entry.

``adapters.model_tool_policy`` merges the shipped file and the overlay; this
module turns the merged profiles into grants for the restricted agent lanes
(``adapters.restricted_tool_lane``) and into status text and an app payload.

A tool on a profile is granted only when it is in the shipped
``restricted_tool_ceiling`` *and* passes the safety predicate
(``domain.model_tool_grants.restricted_tool_refusal``).  A tool that fails is
dropped and reported (``DroppedTool``), never granted and never silently.  A
lane outside ``RESTRICTED_LANES`` is dropped the same way.  A profile with no
tool left is a plain deny entry, so today's behaviour (``tools: []``) is
unchanged.  Granting also needs at least one secret path glob; without one,
every grant is withheld.

The predicate needs facts only the host knows (permission grade, the agent's
operator and execution sets, what the agent dispatcher can run).  They are read
from the loaded ``server`` module, the same way ``tool_lane_guard`` reads it;
when that module is not loaded or lacks one of them, the classification is
unavailable and every grant is withheld with an error (fail closed).

No file is written here.  Thread-safe: the only module state is a cache of
the host's tool sets, one tuple replaced atomically.
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import sonder_runtime.adapters.model_tool_policy as _mtp
from sonder_runtime.domain import model_tool_grants as _rules
from sonder_runtime.domain.tool_lane_models import ToolLaneDenyList, ToolLaneFloor, normalize_name

Classifier = Callable[[str], _rules.ToolFacts]


@dataclass(frozen=True)
class DroppedTool:
    profile_id: str
    tool: str
    reason: str
    source: str

    def text(self) -> str:
        return "profile %r: tool %r (%s) is not granted: %s" % (
            self.profile_id, self.tool, self.source, self.reason,
        )


@dataclass(frozen=True)
class GrantSet:
    """The grants in force, the entries that deny outright, and what was dropped."""

    grants: tuple[_rules.ToolGrant, ...]
    deny_names: frozenset[str]
    deny_prefixes: tuple[str, ...]
    dropped: tuple[DroppedTool, ...]
    errors: tuple[str, ...]
    secret_globs: tuple[str, ...]

    def grant(self, profile_id: str) -> _rules.ToolGrant | None:
        return next((item for item in self.grants if item.profile_id == profile_id), None)


# ---- the host's tool facts ------------------------------------------------------

# ``(key, (risk_of, operator, execution, dispatchable))`` for the loaded host.
_HOST_CACHE: tuple[tuple[int, ...], tuple[Any, ...]] | None = None


def _host_sets(host: Any) -> tuple[Any, ...]:
    global _HOST_CACHE
    dispatch = host._agent_dispatch
    key = (id(host), id(dispatch), id(host.permission_modes))
    cached = _HOST_CACHE
    if cached is not None and cached[0] == key:
        return cached[1]
    permission_modes = host.permission_modes
    generic = host._generic_agent_dispatch
    operator = (
        frozenset(host._AGENT_SYSTEM_OPERATOR_TOOLS)
        | frozenset(generic.RECURSIVE_ENTRYPOINTS)
        | frozenset(generic.ADMIN_NAMES)
        | frozenset(generic.DIRECT_ONLY_NAMES)
    )
    literal = frozenset(host.tool_capabilities.dispatch_names(dispatch, include_generic=False))
    # The restricted lane always runs read-only and bound to a project, so a
    # tool must have its own dispatch branch, pass the repository read-only
    # gate and be one whose path arguments the host confines to the project.
    dispatchable = (
        literal & frozenset(host.REPOSITORY_READ_ONLY_TOOLS) & frozenset(host._PROJECT_SCOPED_PATH_TOOLS)
    )
    sets = (permission_modes.risk_of, operator, frozenset(permission_modes.EXECUTION_TOOLS), dispatchable)
    _HOST_CACHE = (key, sets)
    return sets


def host_classifier() -> tuple[Classifier | None, str]:
    """``(classify, "")`` from the loaded host, or ``(None, why)`` when it cannot be built."""
    host = sys.modules.get("server")
    try:
        risk_of, operator, execution, dispatchable = _host_sets(host)
    except Exception as exc:  # noqa: BLE001 - no classification means no grant
        return None, "tool classification is unavailable (%s); no tool is granted" % type(exc).__name__

    def classify(name: str) -> _rules.ToolFacts:
        try:
            risk = str(risk_of(name))
        except Exception:  # noqa: BLE001 - an unknown grade is not 'safe'
            risk = "unclassified"
        return _rules.ToolFacts(
            name=name, risk=risk, operator=name in operator,
            execution=name in execution, dispatchable=name in dispatchable,
        )

    return classify, ""


def tool_refusal(
    tool: str, config: _mtp.ModelToolConfig | None = None, classify: Classifier | None = None,
) -> str:
    """Why ``tool`` cannot be granted to a restricted model, or ``""``."""
    config = _mtp.load() if config is None else config
    if not config.readable_shipped:
        return "the shipped model tool policy is unavailable"
    if tool not in config.tool_ceiling:
        return "it is not in the shipped restricted_tool_ceiling (%s)" % (
            ", ".join(config.tool_ceiling) or "empty"
        )
    if classify is None:
        classify, why = host_classifier()
        if classify is None:
            return why
    return _rules.restricted_tool_refusal(classify(tool))


# ---- grants ---------------------------------------------------------------------

def grant_set(
    config: _mtp.ModelToolConfig | None = None, classify: Classifier | None = None,
) -> GrantSet:
    """The grants in force for ``config`` (default: the loaded config)."""
    config = _mtp.load() if config is None else config
    errors: list[str] = []
    blocked = ""
    if classify is None:
        classify, blocked = host_classifier()
    globs = config.secret_globs
    if not blocked and not globs:
        blocked = "no restricted_secret_path_globs are configured; no tool is granted"
    if blocked and any(profile.tools for profile in config.profiles):
        errors.append(blocked)
    grants: list[_rules.ToolGrant] = []
    dropped: list[DroppedTool] = []
    deny_names: set[str] = set()
    deny_prefixes: list[str] = []
    for profile in config.profiles:
        kept: list[str] = []
        for entry in profile.tools:
            reason = blocked or tool_refusal(entry.value, config, classify)
            if reason:
                dropped.append(DroppedTool(profile.id, entry.value, reason, entry.source.value))
            else:
                kept.append(entry.value)
        lanes: list[str] = []
        for entry in profile.lanes:
            if entry.value in _rules.RESTRICTED_LANES:
                lanes.append(entry.value)
            else:
                errors.append("profile %r: lane %r (%s) is not a restricted lane (%s); ignored" % (
                    profile.id, entry.value, entry.source.value, ", ".join(sorted(_rules.RESTRICTED_LANES)),
                ))
        names = profile.values("names")
        prefixes = profile.values("prefixes")
        if kept:
            grants.append(_rules.ToolGrant(
                profile_id=profile.id, names=frozenset(names), prefixes=prefixes,
                tools=frozenset(kept), lanes=frozenset(lanes),
            ))
        else:
            deny_names.update(names)
            deny_prefixes.extend(item for item in prefixes if item not in deny_prefixes)
    return GrantSet(
        grants=tuple(grants), deny_names=frozenset(deny_names), deny_prefixes=tuple(deny_prefixes),
        dropped=tuple(dropped), errors=tuple(dict.fromkeys(errors)), secret_globs=globs,
    )


def _legacy_entries(section: Any, floor: ToolLaneFloor) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The legacy section's own entries (normalised; the floor it always echoes removed)."""
    if not isinstance(section, Mapping):
        return (), ()
    names = tuple(dict.fromkeys(
        normalize_name(item) for item in section.get("names") or ()
        if isinstance(item, str) and normalize_name(item) and normalize_name(item) not in floor.names
    ))
    prefixes = tuple(dict.fromkeys(
        normalize_name(item) for item in section.get("prefixes") or ()
        if isinstance(item, str) and normalize_name(item) and normalize_name(item) not in floor.prefixes
    ))
    return names, prefixes


def deny_only_list(grants: GrantSet, floor: ToolLaneFloor, legacy_section: Any) -> ToolLaneDenyList:
    """Every entry that denies outright: profiles with no tool left, then the legacy section."""
    legacy_names, legacy_prefixes = _legacy_entries(legacy_section, floor)
    prefixes = list(grants.deny_prefixes)
    prefixes.extend(item for item in legacy_prefixes if item not in prefixes)
    return ToolLaneDenyList(
        names=grants.deny_names | frozenset(legacy_names), prefixes=tuple(prefixes),
        readable=True, floor=floor,
    )


# ---- status -------------------------------------------------------------------

def _entries_text(entries: Iterable[_mtp.Entry]) -> str:
    return ", ".join("%s (%s)" % (entry.value, entry.source.value) for entry in entries) or "-"


def describe_lines(
    config: _mtp.ModelToolConfig | None = None, legacy_section: Any = None,
    classify: Classifier | None = None,
) -> list[str]:
    """Status lines: each merged profile with the source of every entry, the ceiling, globs and errors."""
    config = _mtp.load() if config is None else config
    grants = grant_set(config, classify)
    lines = ["model tool profiles (restricted lanes: %s):" % ", ".join(sorted(_rules.RESTRICTED_LANES))]
    if not config.profiles:
        lines.append("  (none: the shipped model tool policy is unavailable)")
    for profile in config.profiles:
        grant = grants.grant(profile.id)
        state = (
            "RESTRICTED: %s in %s" % (
                ", ".join(sorted(grant.tools)), ", ".join(sorted(grant.lanes)) or "no lane",
            )
            if grant is not None else "chat by name only"
        )
        lines.append("  %s%s: %s" % (profile.id, " [floor]" if profile.floor else "", state))
        lines.append("    names: %s" % _entries_text(profile.names))
        lines.append("    prefixes: %s" % _entries_text(profile.prefixes))
        lines.append("    tools: %s" % _entries_text(profile.tools))
        lines.append("    lanes: %s" % _entries_text(profile.lanes))
    legacy_names, legacy_prefixes = _legacy_entries(legacy_section, config.floor)
    if legacy_names or legacy_prefixes:
        lines.append("  legacy runtime_policy.json tool_lane_denied_models (legacy, deny only): "
                     "names=%s prefixes=%s" % (", ".join(legacy_names) or "-", ", ".join(legacy_prefixes) or "-"))
    lines.append("  restricted tool ceiling (shipped): %s" % (", ".join(config.tool_ceiling) or "-"))
    lines.append("  restricted secret path globs: %s" % _entries_text(config.secret_path_globs))
    lines.extend("  DROPPED: %s" % item.text() for item in grants.dropped)
    lines.extend("  ERROR: %s" % error for error in grants.errors)
    return lines


def status_payload(
    config: _mtp.ModelToolConfig | None = None, legacy_section: Any = None,
    classify: Classifier | None = None,
) -> dict[str, Any]:
    """The same information as ``describe_lines`` as JSON-ready data (names only, no secrets)."""
    config = _mtp.load() if config is None else config
    grants = grant_set(config, classify)

    def sourced(entries: Iterable[_mtp.Entry]) -> list[dict[str, str]]:
        return [{"value": entry.value, "source": entry.source.value} for entry in entries]

    profiles: list[dict[str, Any]] = []
    for profile in config.profiles:
        grant = grants.grant(profile.id)
        profiles.append({
            "id": profile.id,
            "floor": profile.floor,
            "names": sourced(profile.names),
            "prefixes": sourced(profile.prefixes),
            "tools": sourced(profile.tools),
            "lanes": sourced(profile.lanes),
            "status": "restricted" if grant is not None else "denied",
            "granted_tools": sorted(grant.tools) if grant is not None else [],
            "granted_lanes": sorted(grant.lanes) if grant is not None else [],
        })
    legacy_names, legacy_prefixes = _legacy_entries(legacy_section, config.floor)
    return {
        "shipped_path": config.shipped_path,
        "overlay_path": config.overlay_path,
        "readable_shipped": config.readable_shipped,
        "readable_overlay": config.readable_overlay,
        "overlay_present": config.overlay_present,
        "restricted_lanes": sorted(_rules.RESTRICTED_LANES),
        "profiles": profiles,
        "legacy": {"names": list(legacy_names), "prefixes": list(legacy_prefixes), "source": "legacy"},
        "tool_ceiling": list(config.tool_ceiling),
        "secret_path_globs": sourced(config.secret_path_globs),
        "dropped_tools": [
            {"profile": item.profile_id, "tool": item.tool, "reason": item.reason, "source": item.source}
            for item in grants.dropped
        ],
        "errors": list(config.errors) + list(grants.errors),
    }
