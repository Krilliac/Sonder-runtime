"""``/runtime tools``: the console editor for the operator's model tool overlay.

Reads are open: ``/runtime tools`` shows every merged profile with the source
of each entry (shipped, overlay or legacy), the shipped ceiling, the secret
path globs, every dropped tool and every error.

Writes (``allow``, ``revoke``, ``deny``, ``undeny``) change only the operator
overlay (``<state home>/model_tool_policy.json``) and only when the console
says an operator is attending (``operator_attended=True``, which
``server.control_command`` passes only from the REPL, exactly as ``/approve``
gets ``operator_approved``).  Every other caller (the MCP chat path, HTTP,
an agent) is refused and the file is not touched.  A write takes the file
lock, re-reads the overlay, refuses to overwrite one that is unreadable or
invalid (the operator fixes it by hand), validates the result with the same
rules the loader uses, and replaces the file atomically.

Floors are never edited: a shipped entry (a floor's names, prefixes or any
shipped tool) cannot be revoked or undenied here.  ``allow`` refuses a tool
outside the shipped ceiling or one that fails the safety predicate, naming
why.
"""
from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any

import sonder_runtime.adapters.model_tool_grants as _grants
import sonder_runtime.adapters.model_tool_policy as _mtp
import sonder_runtime.adapters.restricted_tool_lane as _lane
from sonder_runtime.adapters.filesystem import atomic_json as _atomic_json
from sonder_runtime.domain import model_tool_grants as _rules
from sonder_runtime.domain.runtime_policy.rules import _MODEL_RE
from sonder_runtime.domain.tool_lane_models import normalize_name

WRITE_ACTIONS = frozenset({"allow", "revoke", "deny", "undeny", "reload"})
GRANT_PREFIX = "grant-"
DENY_PREFIX = "deny-"
USAGE = (
    "model tool policy commands (writes need the console operator):\n"
    "  /runtime tools                                  show profiles, sources, ceiling, globs, errors\n"
    "  /runtime tools allow <model|prefix*> <tool> [agent|workbench_agent]\n"
    "  /runtime tools revoke <model|prefix*> <tool>\n"
    "  /runtime tools deny <model|prefix*>\n"
    "  /runtime tools undeny <model|prefix*>\n"
    "  /runtime tools reload                           pin the shipped policy file now on disk\n"
    "A model a profile lists is refused in every other tool lane; a granted model gets only\n"
    "read-only project tools, in the listed lanes, with a project root and the secret globs."
)


class _Refused(Exception):
    """A console edit that was not made; the message says why."""


def _refused(action: str, reason: str) -> str:
    message = "refused /runtime tools %s: %s" % (action, reason)
    return message


def command(arg: Any, *, operator_attended: bool, policy: Mapping[str, Any] | None = None) -> str:
    """Run one ``/runtime tools`` command."""
    text = str(arg or "").strip()
    action, _, rest = text.partition(" ")
    action = action.lower()
    if action in ("", "show", "list", "status"):
        return show(policy)
    if action in ("help", "?"):
        return USAGE
    if action not in WRITE_ACTIONS:
        return _refused(action, "unknown action; try /runtime tools help")
    if operator_attended is not True:
        return _refused(action, (
            "the model tool policy is edited only at the console with the operator "
            "present; nothing was changed"
        ))
    words = rest.split()
    handlers: dict[str, Callable[[list[str], Mapping[str, Any] | None], str]] = {
        "allow": _allow, "revoke": _revoke, "deny": _deny, "undeny": _undeny, "reload": _reload,
    }
    try:
        return handlers[action](words, policy)
    except _Refused as exc:
        return _refused(action, str(exc))


def show(policy: Mapping[str, Any] | None = None) -> str:
    config = _mtp.load()
    legacy = policy.get("tool_lane_denied_models") if isinstance(policy, Mapping) else None
    return "\n".join(_mtp.describe(config) + _grants.describe_lines(config, legacy))


def _reload(words: list[str], policy: Mapping[str, Any] | None) -> str:
    """Pin the shipped file as it is now, and show what the operator is accepting.

    The pin exists so that a write nobody guarded cannot change the floor; a
    reload re-pins whatever is on disk, so it prints both hashes and every
    floor name and prefix, restricted ceiling tool, secret path glob,
    undetermined served name and per-profile tool that differs, for the
    operator to compare with the committed file (``git diff`` on the shipped
    policy) before trusting it.
    """
    if words:
        raise _Refused("reload takes no arguments")
    report = _mtp.repin_shipped()
    if not report.available:
        raise _Refused("the shipped file is not usable, so nothing was pinned (%s)" % "; ".join(report.problems))
    lines = [
        "pinned the shipped model tool policy: %s" % report.path,
        "  previous sha256: %s" % (report.old_sha256 or "(none)"),
        "  now pinned     : %s" % report.new_sha256,
    ]
    for label, values in report.changes:
        lines.append("  %s: %s" % (label, ", ".join(repr(value) if not value else value for value in values)))
    if report.old_sha256 == report.new_sha256:
        lines.append("  the file is unchanged since it was pinned")
    return "\n".join(lines)


# ---- selectors and ids --------------------------------------------------------

def _selector(text: str) -> tuple[str, str]:
    """``("names"|"prefixes", normalised value)`` for ``model`` or ``prefix*``."""
    raw = str(text or "").strip()
    is_prefix = raw.endswith("*")
    body = raw[:-1] if is_prefix else raw
    if not body or "*" in body or not _MODEL_RE.fullmatch(body):
        raise _Refused("%r is not a model name or a prefix ending in '*'" % raw)
    value = normalize_name(body)
    if not value:
        raise _Refused("%r is not a model name or a prefix ending in '*'" % raw)
    return ("prefixes" if is_prefix else "names"), value


def _shown(field: str, value: str) -> str:
    return value + "*" if field == "prefixes" else value


def _profile_id(prefix: str, field: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-._")[:40] or "model"
    digest = hashlib.sha256(("%s:%s" % (field, value)).encode("utf-8")).hexdigest()[:8]
    return "%s%s-%s" % (prefix, slug, digest)


def _listing(config: _mtp.ModelToolConfig, field: str, value: str) -> list[_mtp.MergedProfile]:
    return [profile for profile in config.profiles if value in profile.values(field)]


# ---- the overlay write --------------------------------------------------------

def _overlay_profiles(document: dict[str, Any]) -> list[Any]:
    profiles = document.get("model_profiles")
    if not isinstance(profiles, list):
        profiles = []
        document["model_profiles"] = profiles
    return profiles


def _overlay_profile(document: dict[str, Any], profile_id: str) -> dict[str, Any] | None:
    return next(
        (item for item in _overlay_profiles(document) if isinstance(item, dict) and item.get("id") == profile_id),
        None,
    )


def _add_value(container: dict[str, Any], key: str, value: str) -> None:
    values = container.get(key)
    if not isinstance(values, list):
        values = []
        container[key] = values
    if value not in values:
        values.append(value)


def _write(mutate: Callable[[dict[str, Any]], str]) -> str:
    """Apply ``mutate`` to the overlay under its lock; never overwrite a bad overlay."""
    shipped, _issues = _mtp.load_shipped_document()
    if shipped is None:
        raise _Refused("the shipped model tool policy is unavailable; restore it before editing the overlay")
    path = _mtp.overlay_path()
    try:
        with _atomic_json.file_lock(path):
            if path.exists():
                raw, reason = _mtp.read_document(path)
                if reason:
                    raise _Refused("the overlay %s cannot be read (%s); fix or remove it by hand. "
                                   "It was not overwritten." % (path, reason))
                _document, problems = _mtp.overlay_problems(raw, shipped)
                if problems:
                    raise _Refused("the overlay %s is invalid (%s); fix it by hand. It was not "
                                   "overwritten." % (path, "; ".join(problems)))
            else:
                raw = {"version": _mtp.SCHEMA_VERSION, "model_profiles": []}
            document = copy.deepcopy(raw)
            message = mutate(document)
            _document, problems = _mtp.overlay_problems(document, shipped)
            if problems:
                raise _Refused("the change would make the overlay invalid (%s); nothing was written"
                               % "; ".join(problems))
            _atomic_json.write_json_atomic(path, document)
    except RuntimeError as exc:  # the lock timed out
        raise _Refused("the overlay lock is busy (%s); nothing was written" % exc) from exc
    except OSError as exc:
        raise _Refused("the overlay could not be written (%s: %s)" % (type(exc).__name__, exc)) from exc
    finally:
        _mtp.clear_cache()
    return message


def _effect(field: str, value: str, policy: Mapping[str, Any] | None) -> str:
    """One line on what the merged policy now does for this selector."""
    config = _mtp.load()
    if config.issues:
        return "  WARNING: %s" % "; ".join(config.errors)
    if field != "names":
        grants = _grants.grant_set(config)
        lines = []
        for profile in _listing(config, field, value):
            grant = grants.grant(profile.id)
            lines.append("  now: profile %s is %s" % (profile.id, (
                "RESTRICTED to %s in %s" % (", ".join(sorted(grant.tools)), ", ".join(sorted(grant.lanes)) or "no lane")
                if grant is not None else "chat by name only"
            )))
        return "\n".join(lines)
    decision = _lane.decide(value, policy)
    if decision.status is _rules.ModelToolStatus.RESTRICTED:
        return "  now: %s is RESTRICTED to %s in %s (profiles %s)" % (
            value, ", ".join(sorted(decision.tools)), ", ".join(sorted(decision.lanes)) or "no lane",
            ", ".join(decision.profiles),
        )
    return "  now: %s is %s in the restricted lanes" % (value, decision.status.value.upper())


# ---- actions ------------------------------------------------------------------

def _allow(words: list[str], policy: Mapping[str, Any] | None) -> str:
    if len(words) not in (2, 3):
        raise _Refused("usage: /runtime tools allow <model|prefix*> <tool> [agent|workbench_agent]")
    field, value = _selector(words[0])
    tool = words[1].strip()
    lane = words[2].strip() if len(words) == 3 else _rules.AGENT_LANE
    if lane not in _rules.RESTRICTED_LANES:
        raise _Refused("lane %r is not a restricted lane (%s)" % (lane, ", ".join(sorted(_rules.RESTRICTED_LANES))))
    config = _mtp.load()
    if not config.readable_shipped:
        raise _Refused("the shipped model tool policy is unavailable")
    reason = _grants.tool_refusal(tool, config)
    if reason:
        raise _Refused("tool %r cannot be granted to a restricted model: %s" % (tool, reason))
    listing = [profile for profile in _listing(config, field, value) if not profile.id.startswith(DENY_PREFIX)]
    target = listing[0] if listing else None
    profile_id = target.id if target is not None else _profile_id(GRANT_PREFIX, field, value)
    grants = _grants.grant_set(config)
    blocking = [
        profile for profile in config.profiles
        if profile.id != profile_id and _covers(profile, field, value) and grants.grant(profile.id) is None
    ]
    if blocking:
        # A matching profile with no effective tools keeps the model denied
        # whatever is granted here; writing a grant that cannot apply would
        # only mislead.  Name the selector that would work instead.
        raise _Refused("%s is also matched by profile(s) %s, which grant no tools and so keep it denied; "
                       "grant on that profile's own selector (e.g. /runtime tools allow %s %s) or, for an "
                       "overlay deny, /runtime tools undeny first" % (
                           _shown(field, value), ", ".join(profile.id for profile in blocking),
                           _selector_of(blocking[0]), tool,
                       ))

    def mutate(document: dict[str, Any]) -> str:
        entry = _overlay_profile(document, profile_id)
        if entry is None:
            entry = {"id": profile_id}
            if target is None:
                entry["match"] = {field: [value]}
            _overlay_profiles(document).append(entry)
        _add_value(entry, "tools", tool)
        _add_value(entry, "lanes", lane)
        return "granted %s to %s in lane %s (profile %s)." % (tool, _shown(field, value), lane, profile_id)

    message = _write(mutate)
    notes = []
    covering = [profile.id for profile in config.profiles if profile.id != profile_id and _covers(profile, field, value)]
    if covering:
        notes.append("  note: %s is also listed by profile(s) %s; it gets only the tools every one of them "
                     "grants, and a profile with no tools keeps it denied." % (_shown(field, value), ", ".join(covering)))
    return "\n".join([message] + notes + [_effect(field, value, policy)]).rstrip()


def _selector_of(profile: _mtp.MergedProfile) -> str:
    names, prefixes = profile.values("names"), profile.values("prefixes")
    return names[0] if names else (prefixes[0] + "*") if prefixes else profile.id


def _covers(profile: _mtp.MergedProfile, field: str, value: str) -> bool:
    if field != "names":
        return any(value.startswith(prefix) for prefix in profile.values("prefixes"))
    return value in profile.values("names") or any(
        value.startswith(prefix) or value.rsplit("/", 1)[-1].startswith(prefix)
        for prefix in profile.values("prefixes")
    )


def _revoke(words: list[str], policy: Mapping[str, Any] | None) -> str:
    if len(words) != 2:
        raise _Refused("usage: /runtime tools revoke <model|prefix*> <tool>")
    field, value = _selector(words[0])
    tool = words[1].strip()
    config = _mtp.load()
    listing = [profile for profile in _listing(config, field, value) if not profile.id.startswith(DENY_PREFIX)]
    holder = next((profile for profile in listing if tool in profile.values("tools")), None)
    if holder is None:
        raise _Refused("no profile that lists %s grants tool %r" % (_shown(field, value), tool))
    if holder.source_of("tools", tool) is not _mtp.ConfigSource.OVERLAY:
        raise _Refused("tool %r on profile %r comes from the shipped file%s; it cannot be revoked here"
                       % (tool, holder.id, " (a floor profile)" if holder.floor else ""))

    def mutate(document: dict[str, Any]) -> str:
        entry = _overlay_profile(document, holder.id)
        tools = entry.get("tools") if entry is not None else None
        if entry is None or not isinstance(tools, list) or tool not in tools:
            raise _Refused("the overlay no longer lists tool %r on profile %r" % (tool, holder.id))
        tools.remove(tool)
        raw_match = entry.get("match")
        match = raw_match if isinstance(raw_match, dict) else {}
        has_match = any(match.get(key) for key in ("names", "prefixes"))
        if not tools and (holder.id.startswith(GRANT_PREFIX) or not has_match):
            # A console grant (or a shipped profile's extension) with no tool left
            # is removed, so the model returns to what it was before the grant.
            _overlay_profiles(document).remove(entry)
        return "revoked %s from %s (profile %s)." % (tool, _shown(field, value), holder.id)

    return "\n".join([_write(mutate), _effect(field, value, policy)]).rstrip()


def _deny(words: list[str], policy: Mapping[str, Any] | None) -> str:
    if len(words) != 1:
        raise _Refused("usage: /runtime tools deny <model|prefix*>")
    field, value = _selector(words[0])
    profile_id = _profile_id(DENY_PREFIX, field, value)
    config = _mtp.load()
    if config.profile(profile_id) is not None:
        return "%s is already denied by the overlay (profile %s)." % (_shown(field, value), profile_id)

    def mutate(document: dict[str, Any]) -> str:
        _overlay_profiles(document).append({
            "id": profile_id, "match": {field: [value]}, "tools": [], "lanes": [],
        })
        return "denied %s in every tool lane (profile %s); grants no longer apply to it." % (
            _shown(field, value), profile_id,
        )

    return "\n".join([_write(mutate), _effect(field, value, policy)]).rstrip()


def _undeny(words: list[str], policy: Mapping[str, Any] | None) -> str:
    if len(words) != 1:
        raise _Refused("usage: /runtime tools undeny <model|prefix*>")
    field, value = _selector(words[0])
    config = _mtp.load()
    holders = [
        profile for profile in _listing(config, field, value)
        if not profile.values("tools") and profile.source_of(field, value) is _mtp.ConfigSource.OVERLAY
    ]
    if not holders:
        shipped = [profile for profile in _listing(config, field, value)
                   if profile.source_of(field, value) is _mtp.ConfigSource.SHIPPED]
        if shipped:
            raise _Refused("%s is a shipped entry of profile %r%s; it cannot be removed" % (
                _shown(field, value), shipped[0].id, " (a floor)" if shipped[0].floor else "",
            ))
        legacy = policy.get("tool_lane_denied_models") if isinstance(policy, Mapping) else None
        if isinstance(legacy, Mapping) and value in (legacy.get(field) or ()):
            raise _Refused("%s is in the deprecated tool_lane_denied_models section of runtime_policy.json; "
                           "edit that file to remove it" % _shown(field, value))
        if _listing(config, field, value):
            raise _Refused("%s is listed by a profile that grants tools; use /runtime tools revoke"
                           % _shown(field, value))
        covering = [profile for profile in config.profiles if _covers(profile, field, value)]
        if covering:
            raise _Refused("%s is matched by profile %r through %s, not by an overlay deny entry of its "
                           "own; it cannot be removed here" % (
                               _shown(field, value), covering[0].id, _selector_of(covering[0])))
        raise _Refused("the overlay does not deny %s" % _shown(field, value))
    ids = {profile.id for profile in holders}

    def mutate(document: dict[str, Any]) -> str:
        for entry in list(_overlay_profiles(document)):
            if not isinstance(entry, dict) or entry.get("id") not in ids or entry.get("tools"):
                continue
            match = entry.get("match")
            if isinstance(match, dict) and isinstance(match.get(field), list) and value in match[field]:
                match[field].remove(value)
                if not any(match.get(key) for key in ("names", "prefixes")):
                    _overlay_profiles(document).remove(entry)
        return "removed the overlay deny entry for %s (profile %s)." % (_shown(field, value), ", ".join(sorted(ids)))

    return "\n".join([_write(mutate), _effect(field, value, policy)]).rstrip()
